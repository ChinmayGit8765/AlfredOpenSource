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

from alfred.domain.schemas import Collections, Milestone, Roadmap, Win
from alfred.domain.structured import structured_call
from alfred.ports import ClockPort, ModelPort, StorePort

logger = logging.getLogger(__name__)

_MIN_MILESTONES = 3
_MAX_MILESTONES = 7

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
        return [Win.model_validate(_without_key(doc)) for doc in docs]


def _without_key(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_key"}
