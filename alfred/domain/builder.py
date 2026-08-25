"""The Adaptive Agent Builder: from a stated goal to an approved agent.

Core stance, binding: a lapse is data about whether the habit was the
right one, the right size, or the right time; it is never a moral
failure. The builder interrogates the stated goal to find the smallest
true lever, scaffolds by shape, starts almost too small to fail, anchors
to existing cues, respects the WIP limit, and then makes itself
unnecessary. No streak shame, no fake urgency, ever.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from alfred.domain.registry import AgentRegistry
from alfred.domain.schemas import (
    AgentBlueprint,
    AgentManifest,
    BuilderSession,
    BuilderStage,
    Collections,
    Lifecycle,
    Schedule,
    TargetShape,
    load_or_none,
)
from alfred.domain.structured import structured_call
from alfred.errors import AlfredError
from alfred.ports import ClockPort, ModelPort, StorePort

if TYPE_CHECKING:
    from alfred.domain.user_model import UserModelService

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Only behaviours that draw on shared willpower count against the WIP
# limit; a forming skill or project does not crowd out a forming habit.
_WIP_STATES = frozenset({Lifecycle.FORMING, Lifecycle.RESHAPED})
_WIP_SHAPES = frozenset({TargetShape.HABIT, TargetShape.STATE})

_APPROVALS = frozenset(
    {"yes", "y", "yep", "approve", "approved", "ship it", "go ahead", "lgtm", "ok", "okay"}
)
_REJECTIONS = frozenset({"no", "n", "nope", "cancel", "stop", "reject", "drop it"})

# Explicit cancellations work at EVERY stage, so a session can never wedge
# the conversation. Bare "no" stays approval-stage-only: during elicitation
# it is usually an answer to a question, not a cancellation.
_CANCELS = frozenset(
    {"cancel", "stop", "drop it", "abandon", "never mind", "nevermind", "forget it", "quit"}
)

_PROMPT_REQUIRED = ("identity", "scope", "smallest", "anchor", "tone", "output")


class WipVerdict(BaseModel):
    allowed: bool
    forming_count: int
    detail: str = ""


def check_wip(registry: AgentRegistry, *, limit: int = 2) -> WipVerdict:
    """Refuse new habit builds while too many habits are still forming."""
    forming = [
        agent
        for agent in registry.active()
        if agent.manifest.lifecycle in _WIP_STATES
        and agent.manifest.shape in _WIP_SHAPES
    ]
    count = len(forming)
    if count >= limit:
        names = ", ".join(sorted(agent.manifest.name for agent in forming))
        detail = (
            f"{count} habit{'s are' if count != 1 else ' is'} still forming "
            f"right now: {names}. Capacity is finite and willpower is shared, "
            "so stacking another habit now would put the forming ones at risk."
        )
        return WipVerdict(allowed=False, forming_count=count, detail=detail)
    return WipVerdict(allowed=True, forming_count=count, detail="")


# --- private structured-call schemas ---------------------------------------


class _ElicitStep(BaseModel):
    question: str = ""
    satisfied: bool = False
    real_lever: str | None = None


class _ShapeCall(BaseModel):
    shape: TargetShape
    rationale: str = ""


# --- prompts ----------------------------------------------------------------

_STANCE = (
    "A lapse is data about whether the habit was the right one, the right "
    "size, or the right time; it is never a moral failure. Never use streak "
    "pressure, guilt, or fake urgency. Find the smallest true lever, then "
    "make yourself unnecessary."
)

_ELICIT_SYSTEM = (
    "You are ALFRED's Adaptive Agent Builder, in the elicitation phase. "
    + _STANCE
    + " The stated goal is rarely the real lever: 'read more' is often "
    "'get off my phone at night'. Before anything gets built, find the "
    "smallest true lever behind the stated goal. Ask exactly one short, "
    "concrete probing question at a time, about when the goal matters, why "
    "it matters now, and what currently blocks it. When the conversation "
    "makes the real lever clear, set satisfied to true and state the lever "
    "as one plain sentence in real_lever; otherwise set satisfied to false "
    "and ask the next question."
)

_CLASSIFY_SYSTEM = (
    "You are ALFRED's Adaptive Agent Builder, classifying the shape of a "
    "goal. Shapes: habit (a recurring behaviour), skill (deliberate practice "
    "toward competence), project (finite work with an end state), state (a "
    "felt condition to protect or improve, like 'be less anxious'; a state "
    "is not a daily checkbox), metric (a number to move). Pick the single "
    "shape that fits the real lever and give a one-line rationale."
)

_DESIGN_SYSTEM = (
    "You are ALFRED's Adaptive Agent Builder, designing one agent. "
    + _STANCE
    + " Design rules, all binding: start at the smallest viable size, the "
    "version almost too small to fail (the owner can scale up later; "
    "starting big is how habits die); anchor the behaviour to an existing "
    "cue in the owner's day (habit stacking: after X, do Y); habit shapes "
    "get a short daily check-in schedule; allowed_tools stays empty because "
    "the owner grants tools deliberately, later; lifecycle starts at "
    "proposed; name is a short lowercase slug. prompt_md must contain "
    "sections covering: Identity (who the agent is and the single lever it "
    "serves), Scope (what it does and what it will not do), the "
    "smallest-viable-size rule, the Anchor cue, Tone (a lapse is data, "
    "never a moral failure; no streak pressure, no fake urgency), and "
    "Output expectations (short, concrete check-ins). The agent's job is "
    "to make itself unnecessary."
)

_REVISE_SYSTEM = _DESIGN_SYSTEM + (
    " Revise the existing blueprint according to the owner's feedback, "
    "changing only what the feedback requires. Return the full blueprint."
)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    if not slug or not slug[0].isalpha():
        slug = f"agent-{slug}".strip("-_")
    if len(slug) < 2:
        slug = "agent"
    return slug[:41]


def _ensure_prompt_sections(
    prompt_md: str, session: BuilderSession, manifest: AgentManifest
) -> str:
    """Guarantee the prompt carries the binding sections; append when missing."""
    lowered = prompt_md.lower()
    if all(marker in lowered for marker in _PROMPT_REQUIRED):
        return prompt_md
    lever = session.real_lever or session.stated_goal
    block = (
        "\n\n## Operating rules (ALFRED standard)\n"
        f"- Identity: you are '{manifest.name}', the agent for one lever: {lever}.\n"
        f"- Scope: {manifest.description} Nothing beyond that lever; you have "
        "no tools unless the owner grants them.\n"
        "- Smallest viable size: keep the asked-for behaviour almost too small "
        "to fail; scale up only when it has become easy.\n"
        "- Anchor: stack the behaviour onto an existing cue in the owner's day "
        "(after X, do Y).\n"
        "- Tone: a lapse is data, never a moral failure; one miss is fine; no "
        "streak pressure, no fake urgency, no guilt.\n"
        "- Output: short, concrete check-ins; ask for an outcome report in one "
        "line.\n"
    )
    return prompt_md.rstrip() + block


class AgentBuilder:
    """Conversational state machine that designs one agent at a time."""

    def __init__(
        self,
        model: ModelPort,
        user_model: UserModelService,
        store: StorePort,
        clock: ClockPort,
        taken_names: Callable[[], set[str]] | None = None,
    ) -> None:
        self._model = model
        self._user_model = user_model
        self._store = store
        self._clock = clock
        # The registry only knows folders that LOADED. A folder on disk
        # with a broken manifest is invisible to it, and naming a new agent
        # after one makes materialisation fail after approval. The injected
        # provider (wired in composition, like any port) reports what is
        # actually on disk so naming avoids it up front.
        self._taken_names = taken_names

    # --- public surface -----------------------------------------------------

    async def start(
        self, stated_goal: str, registry: AgentRegistry
    ) -> tuple[BuilderSession, str]:
        now = self._clock.now()
        session = BuilderSession(stated_goal=stated_goal, created_at=now, updated_at=now)
        session.transcript.append({"role": "owner", "text": stated_goal})

        verdict = check_wip(registry)
        if not verdict.allowed:
            session.stage = BuilderStage.ABANDONED
            message = (
                f"{verdict.detail} I would rather protect those than stack a "
                "new one on top. Happy to revisit this goal once one of them "
                "is established, or we can shrink, pause, or retire one now "
                "to make room. Your call."
            )
            session.transcript.append({"role": "alfred", "text": message})
            await self._save(session)
            logger.info("builder refused at WIP limit (%d forming)", verdict.forming_count)
            return session, message

        step = await structured_call(
            self._model,
            schema=_ElicitStep,
            system=_ELICIT_SYSTEM,
            user=(
                f"Stated goal: {stated_goal}\n"
                "Ask your first probing question to find the real lever."
            ),
        )
        question = step.question or (
            "What would actually change in your day if this goal were handled?"
        )
        session.transcript.append({"role": "alfred", "text": question})
        await self._save(session)
        return session, question

    async def step(
        self, session_id: str, owner_message: str, registry: AgentRegistry
    ) -> tuple[BuilderSession, str]:
        session = await self.get_session(session_id)
        if session is None:
            raise AlfredError(f"unknown builder session: {session_id}")
        session.transcript.append({"role": "owner", "text": owner_message})

        if (
            _normalise(owner_message) in _CANCELS
            and session.stage not in (BuilderStage.DONE, BuilderStage.ABANDONED)
        ):
            session.stage = BuilderStage.ABANDONED
            message = (
                "Dropped, no harm done. Deciding not to build something is a "
                "good outcome too. Come back to it whenever it earns its place."
            )
            session.transcript.append({"role": "alfred", "text": message})
            await self._save(session)
            return session, message

        match session.stage:
            case BuilderStage.ELICITING:
                message = await self._step_eliciting(session)
            case BuilderStage.CLASSIFYING:
                message = await self._classify_and_advance(session)
            case BuilderStage.DESIGNING:
                message = await self._step_designing(session, owner_message, registry)
            case BuilderStage.CAPACITY_CHECK:
                message = await self._step_capacity(session, owner_message, registry)
            case BuilderStage.PROPOSING:
                message = self._propose(session)
            case BuilderStage.AWAITING_APPROVAL:
                message = await self._step_approval(session, owner_message, registry)
            case _:
                message = (
                    "That building session is closed. Say 'new agent <goal>' "
                    "whenever you want to start another."
                )

        session.transcript.append({"role": "alfred", "text": message})
        await self._save(session)
        return session, message

    async def get_session(self, session_id: str) -> BuilderSession | None:
        doc = await self._store.get(Collections.BUILDER_SESSIONS, session_id)
        if doc is None:
            return None
        doc = dict(doc)
        doc.pop("_key", None)
        return BuilderSession.model_validate(doc)

    async def active_session(self) -> BuilderSession | None:
        docs = await self._store.query(Collections.BUILDER_SESSIONS)
        open_sessions: list[BuilderSession] = []
        for doc in docs:
            # Closed sessions are dead history: filter on the RAW stage
            # before validating, so a drifted legacy row from an older
            # release can never brick every non-command message (this is
            # called on each one). A missing stage falls through to
            # validation: the model default is ELICITING, which is open.
            if doc.get("stage") in (
                BuilderStage.DONE.value,
                BuilderStage.ABANDONED.value,
            ):
                continue
            session = load_or_none(
                BuilderSession, doc, source=Collections.BUILDER_SESSIONS
            )
            if session is None:
                continue
            if session.stage not in (BuilderStage.DONE, BuilderStage.ABANDONED):
                open_sessions.append(session)
        if not open_sessions:
            return None
        return max(open_sessions, key=lambda s: s.updated_at or s.created_at or _EPOCH)

    async def reopen(self, session_id: str) -> BuilderSession | None:
        """Return an approved session to AWAITING_APPROVAL after a failed write.

        Approval marks the session DONE before the runtime writes the
        folder; when that write fails (name collision with a loader-invisible
        folder, disk error) the elicited lever and blueprint must survive so
        the owner can rename or retry instead of redoing the conversation.
        Returns None when the session is unknown or holds no blueprint.
        """
        session = await self.get_session(session_id)
        if session is None or session.blueprint is None:
            return None
        session.stage = BuilderStage.AWAITING_APPROVAL
        # Nothing went live, so the approval's lifecycle flip is undone.
        session.blueprint.manifest.lifecycle = Lifecycle.PROPOSED
        session.transcript.append(
            {
                "role": "alfred",
                "text": "(writing the agent folder failed; the build is open again)",
            }
        )
        await self._save(session)
        return session

    async def abandon_active(self) -> str | None:
        """Abandon the most recent open session; returns its id when one existed.

        Used when the owner starts a fresh build while one is open: the old
        session must not resurrect later and intercept messages.
        """
        session = await self.active_session()
        if session is None:
            return None
        session.stage = BuilderStage.ABANDONED
        session.transcript.append(
            {"role": "alfred", "text": "(superseded by a new building session)"}
        )
        await self._save(session)
        return session.id

    # --- stage handlers -----------------------------------------------------

    async def _step_eliciting(self, session: BuilderSession) -> str:
        step = await structured_call(
            self._model,
            schema=_ElicitStep,
            system=_ELICIT_SYSTEM,
            user=self._transcript_text(session),
        )
        if step.satisfied and step.real_lever:
            session.real_lever = step.real_lever
            session.stage = BuilderStage.CLASSIFYING
            return await self._classify_and_advance(session)
        return step.question or (
            "Tell me more about when this bites during a normal day."
        )

    async def _classify_and_advance(self, session: BuilderSession) -> str:
        result = await structured_call(
            self._model,
            schema=_ShapeCall,
            system=_CLASSIFY_SYSTEM,
            user=(
                f"Stated goal: {session.stated_goal}\n"
                f"Real lever: {session.real_lever}\n\n"
                + self._transcript_text(session)
            ),
        )
        session.shape = result.shape
        session.stage = BuilderStage.DESIGNING
        rationale = f" ({result.rationale})" if result.rationale else ""
        return (
            f"Here is what I heard: the real lever is '{session.real_lever}'. "
            f"That is a {result.shape.value}{rationale}. If that lands, say so "
            "and I will design the smallest agent that moves it; if not, "
            "correct me."
        )

    async def _step_designing(
        self, session: BuilderSession, owner_message: str, registry: AgentRegistry
    ) -> str:
        blueprint = await structured_call(
            self._model,
            schema=AgentBlueprint,
            system=_DESIGN_SYSTEM,
            user=(
                f"Stated goal: {session.stated_goal}\n"
                f"Real lever: {session.real_lever}\n"
                f"Shape: {session.shape.value if session.shape else 'unknown'}\n"
                f"Owner's latest message: {owner_message}\n\n"
                "Design the blueprint now."
            ),
        )
        session.blueprint = self._enforce(blueprint, session, registry)
        session.stage = BuilderStage.CAPACITY_CHECK
        # Fresh design always gets an honest capacity look before proposing;
        # the empty message means no override is possible on this pass.
        return await self._step_capacity(session, "", registry)

    async def _step_capacity(
        self, session: BuilderSession, owner_message: str, registry: AgentRegistry
    ) -> str:
        blueprint = session.blueprint
        if blueprint is None:
            session.stage = BuilderStage.DESIGNING
            return "I lost the draft blueprint; tell me to design again."

        # An owner message here that is neither the internal re-check (empty)
        # nor the explicit "force" override is revision feedback: shrink or
        # drop to fit, exactly what the refusal invites. Without this the
        # owner would be wedged between forcing past capacity and cancelling.
        if owner_message.strip() and _normalise(owner_message) != "force":
            return await self._revise(session, owner_message, registry)

        # The override must be the whole message: "force" deliberately typed,
        # never a substring of something like "don't force it".
        forced = _normalise(owner_message) == "force"
        wip = check_wip(registry)
        profile = await self._user_model.get_profile()
        active_cost = sum(a.manifest.capacity_cost for a in registry.active())
        total = active_cost + blueprint.manifest.capacity_cost
        fits = wip.allowed and total <= profile.weekly_capacity

        if not fits and not forced:
            problems: list[str] = []
            if not wip.allowed:
                problems.append(wip.detail)
            if total > profile.weekly_capacity:
                problems.append(
                    f"your active agents already cost {active_cost} capacity "
                    f"points and this one adds "
                    f"{blueprint.manifest.capacity_cost}, for {total} against "
                    f"a weekly budget of {profile.weekly_capacity}"
                )
            return (
                "I will be honest: this does not fit right now. "
                + " Also, ".join(problems)
                + ". We can drop or shrink something to make room, or say "
                "'force' to go ahead anyway with eyes open."
            )
        if forced and not fits:
            logger.info("owner forced past capacity check for %s", blueprint.manifest.name)
        return self._propose(session)

    def _propose(self, session: BuilderSession) -> str:
        session.stage = BuilderStage.AWAITING_APPROVAL
        return self._render_blueprint(session)

    async def _step_approval(
        self, session: BuilderSession, owner_message: str, registry: AgentRegistry
    ) -> str:
        blueprint = session.blueprint
        if blueprint is None:
            session.stage = BuilderStage.DESIGNING
            return "I lost the draft blueprint; tell me to design again."

        word = _normalise(owner_message)
        if word in _APPROVALS:
            blueprint.manifest.lifecycle = Lifecycle.FORMING
            session.stage = BuilderStage.DONE
            logger.info("blueprint approved: %s", blueprint.manifest.name)
            return (
                f"Done. '{blueprint.manifest.name}' is approved and starts "
                "forming now. I will check in daily while it beds in, and one "
                "miss will always be fine."
            )
        if word in _REJECTIONS:
            session.stage = BuilderStage.ABANDONED
            return (
                "Dropped, no harm done. Deciding not to build something is a "
                "good outcome too. Come back to it whenever it earns its place."
            )

        return await self._revise(session, owner_message, registry)

    async def _revise(
        self, session: BuilderSession, owner_message: str, registry: AgentRegistry
    ) -> str:
        """Revise the current blueprint per the owner's feedback, then re-check.

        Shared by the approval stage and the capacity-check stage so an owner
        can shrink or drop to fit at either point.
        """
        blueprint = session.blueprint
        assert blueprint is not None  # callers guarantee this
        revised = await structured_call(
            self._model,
            schema=AgentBlueprint,
            system=_REVISE_SYSTEM,
            user=(
                f"Current blueprint JSON:\n{blueprint.model_dump_json()}\n\n"
                f"Owner feedback: {owner_message}\n"
                "Return the full revised blueprint."
            ),
        )
        session.blueprint = self._enforce(revised, session, registry)
        # A revision can change capacity_cost, so it goes back through the
        # honest capacity look instead of straight to approval.
        session.stage = BuilderStage.CAPACITY_CHECK
        return "Revised. " + await self._step_capacity(session, "", registry)

    # --- helpers ------------------------------------------------------------

    def _enforce(
        self, blueprint: AgentBlueprint, session: BuilderSession, registry: AgentRegistry
    ) -> AgentBlueprint:
        """Apply the non-negotiable blueprint rules after model validation."""
        manifest = blueprint.manifest
        manifest.lifecycle = Lifecycle.PROPOSED
        # Least privilege: the owner grants tools deliberately, never pre-granted.
        manifest.allowed_tools = []
        if manifest.shape is None and session.shape is not None:
            manifest.shape = session.shape
        if manifest.shape is TargetShape.HABIT:
            if manifest.schedule.kind != "daily":
                manifest.schedule = Schedule(
                    kind="daily", time=manifest.schedule.time or "08:00"
                )
            manifest.capacity_cost = max(1, min(4, manifest.capacity_cost))
        manifest.name = self._free_name(_slugify(manifest.name), registry)
        blueprint.prompt_md = _ensure_prompt_sections(
            blueprint.prompt_md, session, manifest
        )
        return blueprint

    def _free_name(self, base: str, registry: AgentRegistry) -> str:
        taken = (
            {name.lower() for name in self._taken_names()}
            if self._taken_names is not None
            else set()
        )

        def free(candidate: str) -> bool:
            return registry.get(candidate) is None and candidate.lower() not in taken

        if free(base):
            return base
        n = 2
        while True:
            candidate = f"{base[:37]}-{n}"
            if free(candidate):
                return candidate
            n += 1

    def _render_blueprint(self, session: BuilderSession) -> str:
        blueprint = session.blueprint
        assert blueprint is not None  # callers guarantee this
        manifest = blueprint.manifest
        schedule = manifest.schedule
        when = {
            "daily": "a short daily check-in"
            + (f" at {schedule.time}" if schedule.time else ""),
            "weekly": "a weekly check-in",
            "interval": "periodic check-ins",
            "none": "no scheduled check-ins; it responds when you bring it up",
        }[schedule.kind]
        anchor = next(
            (
                line.strip("-* ").strip()
                for line in blueprint.prompt_md.splitlines()
                if "anchor" in line.lower() and line.strip()
            ),
            "stacks onto an existing cue in your day (written into its prompt)",
        )
        return "\n".join(
            [
                f"Proposal: agent '{manifest.name}'.",
                f"- What: {manifest.description}",
                f"- Real lever: {session.real_lever or session.stated_goal}",
                f"- When: {when}",
                (
                    "- How small: starts at the smallest viable size, costing "
                    f"{manifest.capacity_cost} of your weekly capacity points"
                ),
                f"- Anchor: {anchor}",
                (
                    "- What it will NOT do: it has no tool access until you "
                    "grant it, it will not nag, and it will never use streak "
                    "pressure or fake urgency."
                ),
                "Say 'yes' to ship it, 'no' to drop it, or tell me what to change.",
            ]
        )

    @staticmethod
    def _transcript_text(session: BuilderSession) -> str:
        lines = [f"Stated goal: {session.stated_goal}", "", "Conversation so far:"]
        lines.extend(
            f"{entry.get('role', '?')}: {entry.get('text', '')}"
            for entry in session.transcript
        )
        return "\n".join(lines)

    async def _save(self, session: BuilderSession) -> None:
        session.updated_at = self._clock.now()
        await self._store.put(
            Collections.BUILDER_SESSIONS, session.id, session.model_dump(mode="json")
        )
