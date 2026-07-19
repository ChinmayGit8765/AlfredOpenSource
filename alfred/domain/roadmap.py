"""Roadmap to goal: a destination decomposed into many small wins.

ALFRED's headline move is to take a goal and lay a path of milestones that
are each almost too small to fail, then walk beside the owner through them
one at a time. RoadmapPlanner produces the path via a structured call;
WinsLedger keeps the running log of wins so progress stays visible. The
stance is binding: celebrate progress, surface one next step, never the
whole mountain, and never use streaks or shame for pressure.
"""

from __future__ import annotations

import logging

from alfred.domain.schemas import Collections, Milestone, Roadmap, Win, load_or_none
from alfred.domain.structured import structured_call
from alfred.ports import ClockPort, ModelPort, StorePort

logger = logging.getLogger(__name__)

_MIN_MILESTONES = 3
_MAX_MILESTONES = 7

# The owner has one live path at a time; it sits at this well-known key, the
# same "current" convention the user profile uses, so the runtime always
# knows where to find it without a time-ordered scan.
_CURRENT_KEY = "current"

_PLANNER_SYSTEM = (
    "You are ALFRED, laying out a path to a goal as a sequence of small wins. "
    "Binding stance: progress is built from many small wins, not one heroic "
    f"push. Decompose the goal into {_MIN_MILESTONES} to {_MAX_MILESTONES} "
    "milestones, each a single concrete step the owner can finish and feel, "
    "almost too small to fail. The FIRST milestone must be doable today with "
    "what the owner already has. Order them so each win makes the next "
    "easier. For every milestone give: a title (the small win, phrased as a "
    "done-able action), why (one line on how it ladders to the goal), a "
    "done_signal (the observable sign it is complete, so a win is "
    "unambiguous), and an anchor (an existing cue in the owner's day it "
    "stacks onto). Never pad the list to look impressive; fewer, truer steps "
    "beat a long ladder. A lapse is data, never a moral failure; never invoke "
    "streaks, guilt, or urgency. Respond with a single Roadmap JSON object."
)


class RoadmapPlanner:
    """Turns a goal into a roadmap of small, sequenced wins."""

    def __init__(self, model: ModelPort, clock: ClockPort) -> None:
        self._model = model
        self._clock = clock

    async def plan(
        self, goal: str, *, real_lever: str = "", context: str = ""
    ) -> Roadmap:
        user_lines = [f"Goal: {goal}"]
        if real_lever:
            user_lines.append(f"The real lever behind it: {real_lever}")
        if context:
            user_lines.append(f"What I know about the owner:\n{context}")
        user_lines.append(
            "Lay out the smallest honest sequence of wins to get there."
        )
        roadmap = await structured_call(
            self._model,
            schema=Roadmap,
            system=_PLANNER_SYSTEM,
            user="\n".join(user_lines),
        )
        return self._enforce(roadmap, goal, real_lever)

    def _enforce(self, roadmap: Roadmap, goal: str, real_lever: str) -> Roadmap:
        """Apply the non-negotiable shape after model validation."""
        roadmap.goal = goal
        roadmap.real_lever = real_lever or roadmap.real_lever
        roadmap.created_at = self._clock.now()
        # Exactly one active milestone: the owner faces one next step, never
        # the whole mountain. Everything after it waits its turn.
        seen_active = False
        for milestone in roadmap.milestones:
            if milestone.status == "won":
                continue
            if not seen_active:
                milestone.status = "active"
                seen_active = True
            else:
                milestone.status = "pending"
        return roadmap

    async def save(self, roadmap: Roadmap, store: StorePort) -> None:
        await store.put(
            Collections.ROADMAPS, roadmap.id, roadmap.model_dump(mode="json")
        )


class WinsLedger:
    """Records and surfaces small wins so momentum is always visible."""

    def __init__(self, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    async def record(
        self, text: str, *, source: str = "owner", goal: str | None = None
    ) -> Win:
        win = Win(text=text, source=source, goal=goal, at=self._clock.now())
        await self._store.append(Collections.WINS, win.model_dump(mode="json"))
        logger.info("win recorded (source=%s): %.80s", source, text)
        return win

    async def recent(self, limit: int = 10) -> list[Win]:
        docs = await self._store.query(
            Collections.WINS, limit=limit, newest_first=True
        )
        loaded = [load_or_none(Win, doc, source=Collections.WINS) for doc in docs]
        return [win for win in loaded if win is not None]


class RoadmapService:
    """The owner's one live path to a goal: set it, see the next win, advance.

    Sits over the planner and the wins ledger and owns the active roadmap's
    lifecycle. Exactly one roadmap is active at a time, persisted at a
    well-known key so every transport and the heartbeat read the same path.
    Setting a new goal archives the previous roadmap by id rather than
    discarding it: an abandoned path is data, never waste. Completing the
    active milestone records a win and promotes the next one, so the owner
    always faces a single next step, never the whole mountain.
    """

    def __init__(
        self,
        planner: RoadmapPlanner,
        wins: WinsLedger,
        store: StorePort,
        clock: ClockPort,
    ) -> None:
        self._planner = planner
        self._wins = wins
        self._store = store
        self._clock = clock

    async def current(self) -> Roadmap | None:
        """The active roadmap, or None when the owner has set no goal yet."""
        doc = await self._store.get(Collections.ROADMAPS, _CURRENT_KEY)
        if doc is None:
            return None
        roadmap = load_or_none(Roadmap, doc, source=Collections.ROADMAPS)
        if roadmap is None:
            # set_goal overwrites the current key, so silently returning
            # None over a drifted roadmap would let the next goal destroy
            # the only copy of the owner's path. Quarantine it first.
            await self._store.put(
                Collections.ROADMAPS,
                f"unreadable-{self._clock.now().isoformat()}",
                _without_key(doc),
            )
            logger.error(
                "stored roadmap no longer validates; quarantined a copy, "
                "treating the goal as unset"
            )
        return roadmap

    async def set_goal(
        self, goal: str, *, real_lever: str = "", context: str = ""
    ) -> Roadmap:
        """Lay a fresh path to a goal, replacing and archiving any current one."""
        previous = await self.current()
        if previous is not None:
            # Keep the abandoned path by its id: changing direction is data,
            # and the record should survive it.
            await self._planner.save(previous, self._store)
        roadmap = await self._planner.plan(
            goal, real_lever=real_lever, context=context
        )
        await self._save_current(roadmap)
        return roadmap

    async def complete_next(
        self,
    ) -> tuple[Roadmap | None, Milestone | None, Milestone | None]:
        """Mark the active small win won, log it, and promote the next one.

        Returns (roadmap, the milestone just won, the new next milestone). A
        None won-milestone means there was nothing left to win (no roadmap, or
        every milestone already done); the caller renders accordingly.
        """
        roadmap = await self.current()
        if roadmap is None:
            return None, None, None
        won = roadmap.next_win
        if won is None:
            return roadmap, None, None
        won.status = "won"
        await self._wins.record(won.title, source="milestone", goal=roadmap.goal)
        # next_win now skips the just-won milestone; promote the first still-open
        # one so exactly one step is active again.
        new_next = roadmap.next_win
        if new_next is not None:
            new_next.status = "active"
        await self._save_current(roadmap)
        return roadmap, won, new_next

    async def record_win(self, text: str, *, source: str = "owner") -> Win:
        """Log a standalone win to the momentum ledger, tied to the current goal."""
        roadmap = await self.current()
        goal = roadmap.goal if roadmap is not None else None
        return await self._wins.record(text, source=source, goal=goal)

    async def recent_wins(self, limit: int = 10) -> list[Win]:
        return await self._wins.recent(limit=limit)

    async def _save_current(self, roadmap: Roadmap) -> None:
        await self._store.put(
            Collections.ROADMAPS, _CURRENT_KEY, roadmap.model_dump(mode="json")
        )


def _without_key(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_key"}
