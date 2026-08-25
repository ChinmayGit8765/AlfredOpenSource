"""AlfredCore: the assembled brain.

One inbound message flows through a fixed pipeline: log it, try owner
commands, continue any open builder session, then route to agents. Every
reply leaves through the transport, every failure is logged and surfaced
to the owner as a short honest notice, and nothing here ever widens
access: confirmation and approval flows only call into governance.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path

from alfred.config import AlfredConfig
from alfred.domain.builder import AgentBuilder
from alfred.domain.conductor import Conductor
from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.executor import AgentExecutor
from alfred.domain.feedback import adherence_signal, parse_outcome_report
from alfred.domain.governance import PendingActions, Proposals, WorkflowTrust, audit
from alfred.domain.lifecycle import LapseDoctor, lapse_proposal
from alfred.domain.memory import MemoryService
from alfred.domain.reflection import ReflectionEngine
from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.roadmap import RoadmapService
from alfred.domain.routing import route
from alfred.domain.schemas import (
    AdherenceStats,
    AgentBlueprint,
    BuilderStage,
    Collections,
    ExecutionResult,
    InboundMessage,
    LapseDiagnosis,
    Lifecycle,
    Milestone,
    Outcome,
    PendingAction,
    Plan,
    Proposal,
    ProposalKind,
    ReconciledSchedule,
    Reflection,
    Roadmap,
    ScheduledTrigger,
)
from alfred.domain.user_model import UserModelService
from alfred.errors import AlfredError
from alfred.ports import ClockPort, OutboundMessage, StorePort, TransportPort
from alfred.ports.tools import CapabilityTier
from alfred.runtime.agent_loader import materialise_agent, render_manifest_yaml

logger = logging.getLogger(__name__)

_STOP_PHRASE = "alfred stop"

_HELP_TEXT = "\n".join(
    [
        "ALFRED commands:",
        "- help: this summary",
        "- status: active agents, adherence, pending actions and proposals",
        "- goal <goal>: lay a path to a goal as a sequence of small wins",
        "- roadmap: show the path and your one next small win",
        "- next: just the single next small win",
        "- win: mark the next small win done (or 'win <text>' to log a side win)",
        "- wins: your recent wins, newest first",
        "- agents: list every known agent",
        "- pending: list gated tool actions awaiting your confirmation",
        "- confirm <id> / deny <id>: rule on a gated action, or on a whole",
        "  composed intent at once when several actions share one intent id",
        "- trust: the autonomy dial and every workflow's standing",
        "- distrust <agent> <tool>: make one workflow preview again",
        "- proposals: list pending self-change proposals",
        "- approve <id> [confirm-safety] / reject <id>: rule on a proposal",
        "- reflect: run a strategy review now",
        "- remember <fact>: file something ALFRED should bring up later",
        "- recall <topic> (or 'what do you know about <topic>'): search memories",
        "- memories: recent memories; forget <id> deletes one",
        "- new agent <goal> (or optimise <goal>): build a new agent",
        "- alfred stop: shut down cleanly",
        "Anything else routes to your agents by their trigger words.",
    ]
)

_CHECK_IN_TEXT = (
    "Scheduled check-in: ask about progress on the current plan, briefly. "
    "If the latest outcomes show wobble, address it per your rules."
)

_PLANNING_TEXT = (
    "Scheduled planning run: produce this week's plan, consistent with "
    "recent outcomes and capacity."
)


class AlfredCore:
    """Owner commands, builder continuity, routing, and scheduled runs."""

    def __init__(
        self,
        registry: AgentRegistry,
        executor: AgentExecutor,
        conductor: Conductor,
        builder: AgentBuilder,
        user_model: UserModelService,
        dispatcher: ToolDispatcher,
        pending: PendingActions,
        proposals: Proposals,
        reflection: ReflectionEngine,
        lapse_doctor: LapseDoctor,
        roadmap: RoadmapService,
        store: StorePort,
        clock: ClockPort,
        transport: TransportPort,
        config: AlfredConfig,
        agents_dir: Path,
        memory: MemoryService | None = None,
        skipped_agents: list[tuple[str, str]] | None = None,
        trust: WorkflowTrust | None = None,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._conductor = conductor
        self._builder = builder
        self._user_model = user_model
        self._dispatcher = dispatcher
        self._pending = pending
        self._proposals = proposals
        self._reflection = reflection
        self._lapse_doctor = lapse_doctor
        self._roadmap = roadmap
        self._store = store
        self._clock = clock
        self._transport = transport
        self._config = config
        self._agents_dir = Path(agents_dir)
        self._memory = memory or MemoryService(store, clock)
        # Threshold 0 when nothing is wired: the dial exists but is off.
        self._trust = trust or WorkflowTrust(store, clock, threshold=0)
        # Folders that failed to load at startup: shown in 'agents' and
        # 'status' so a version rollback can never silently unload agents.
        self._skipped_agents = list(skipped_agents or [])
        # ALFRED serves one owner on one event loop, but every transport plus
        # the heartbeat run as concurrent tasks. This lock serializes every
        # inbound and scheduled handler so their read-modify-write cycles on
        # shared state (pending actions, proposals, builder sessions, the
        # registry) cannot interleave: e.g. a 'confirm <id>' arriving on two
        # channels can no longer execute a gated tool twice. Throughput is a
        # non-goal for a single-owner system; coherence is the point.
        self._handler_lock = asyncio.Lock()
        # An Event rather than a bare bool so the service supervisor can
        # await the kill switch instead of polling it once a second. The
        # stop_requested property below keeps the old attribute contract.
        self._stop_event = asyncio.Event()
        # Freshest channel the owner spoke on; scheduled output goes there
        # when no channel is configured. In-memory on purpose: a stale "cli"
        # channel from a previous chat session must not capture Discord-mode
        # proactive sends.
        self._last_channel: str | None = None

    # --- shutdown -------------------------------------------------------

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    @stop_requested.setter
    def stop_requested(self, requested: bool) -> None:
        if requested:
            self._stop_event.set()
        else:
            self._stop_event.clear()

    async def wait_for_stop(self) -> None:
        """Block until the owner's kill switch is pulled."""
        await self._stop_event.wait()

    # --- entry points ---------------------------------------------------

    async def handle_inbound(self, message: InboundMessage) -> None:
        async with self._handler_lock:
            await self._handle_inbound(message)

    async def _handle_inbound(self, message: InboundMessage) -> None:
        try:
            await self._store.append(
                Collections.MESSAGES, message.model_dump(mode="json")
            )
            text = message.text.strip()

            # Commands, builder sessions, and outcome logging are owner
            # authority. Content from connectors (provenance "external")
            # gets agent routing only; it can never confirm an action,
            # approve a proposal, or steer a build.
            if message.provenance == "owner":
                self._last_channel = message.channel
                if await self._try_command(message, text):
                    return
                if await self._try_builder_continuation(message, text):
                    return
            await self._route_and_run(message, text)
        except Exception as exc:
            logger.exception("handle_inbound failed for message %s", message.id)
            try:
                # AlfredError messages are owner-readable by construction
                # (adapters keep keys and tokens out of error text; keep it
                # that way when adding raise sites), so a remote owner gets
                # the actual hint instead of a class name and a pointer at
                # a log on a machine they may not be sitting at.
                detail = (
                    str(exc)
                    if isinstance(exc, AlfredError) and str(exc)
                    else f"{type(exc).__name__}; the details are in my log"
                )
                await self._send(
                    message.channel,
                    f"Sorry, something went wrong while handling that ({detail}).",
                )
            except Exception:
                logger.exception("failed to deliver the error notice")

    async def run_scheduled(self, trigger: ScheduledTrigger) -> None:
        async with self._handler_lock:
            await self._run_scheduled(trigger)

    async def _run_scheduled(self, trigger: ScheduledTrigger) -> None:
        channel = self._scheduled_channel()
        if trigger.reason == "reflection":
            reflection = await self._reflection.reflect(
                self._registry, self._config.heartbeat.reflection_days
            )
            await self._send_scheduled(channel, self._render_reflection(reflection))
            return

        if trigger.reason == "roadmap_nudge":
            await self._nudge_roadmap(channel)
            return

        agent = self._registry.get(trigger.agent)
        if agent is None:
            logger.warning(
                "scheduled trigger for unknown agent %r (reason=%s); skipping",
                trigger.agent,
                trigger.reason,
            )
            return

        # A lapsing agent gets a diagnosis, not a nag. LAPSING is only reached
        # at two consecutive misses, so a check-in here is the spec's "run a
        # short diagnostic, then shrink/re-anchor/pause/reshape/retire" loop,
        # never a generic prod.
        if (
            trigger.reason == "check_in"
            and agent.manifest.lifecycle is Lifecycle.LAPSING
        ):
            await self._diagnose_lapse(agent, channel)
            return

        text = _CHECK_IN_TEXT if trigger.reason == "check_in" else _PLANNING_TEXT
        result = await self._executor.run(agent, text=text, provenance="scheduler")
        if channel is not None:
            await self._deliver_result(channel, result)
        else:
            # No destination yet (no configured channel, owner not seen
            # this process): a gated action would otherwise expire without
            # the owner ever learning its id existed.
            if result.pending:
                logger.warning(
                    "scheduled run for %s gated %d action(s) with no "
                    "destination; ids: %s; say 'pending' on any channel to "
                    "list them",
                    agent.manifest.name,
                    len(result.pending),
                    [action.id for action in result.pending],
                )
            if result.replies:
                logger.warning(
                    "scheduled run for %s produced %d repl(y/ies) with no "
                    "destination; dropped",
                    agent.manifest.name,
                    len(result.replies),
                )

        # Staggered Monday planning runs still land in one coherent week:
        # once two or more agents have plans for the same week, the
        # Conductor reconciles them. The result is persisted always and
        # surfaced only when it changed something.
        if trigger.reason == "schedule" and result.plan is not None:
            plans = await self._latest_plans_for_week(result.plan.week_of)
            if len(plans) >= 2:
                profile = await self._user_model.get_profile()
                schedule = await self._conductor.reconcile(plans, profile)
                await self._store.append(
                    Collections.SCHEDULES, schedule.model_dump(mode="json")
                )
                if schedule.adjustments or schedule.warnings:
                    await self._send_scheduled(
                        channel, self._render_schedule(schedule)
                    )

    async def _diagnose_lapse(
        self, agent: LoadedAgent, channel: str | None
    ) -> None:
        """Run the lapse doctor and surface one human-in-the-loop proposal.

        A lapse is data, never a moral failure: the diagnosis names the
        likely cause and recommends the smallest true fix, which becomes a
        pending proposal the owner rules on. Nothing changes without approval.
        Skipped while a proposal for this agent is already pending so the
        daily LAPSING check-in cannot nag or pile up duplicate proposals.
        """
        name = agent.manifest.name
        pending = await self._proposals.list_pending()
        if any(p.agent == name for p in pending):
            return

        profile = await self._user_model.get_profile()
        stats = profile.adherence.get(name, AdherenceStats())
        recent = await self._user_model.recent_outcomes(agent=name, limit=5)
        diagnosis = await self._lapse_doctor.diagnose(agent, stats, recent)

        lines = [self._render_diagnosis(name, diagnosis)]
        proposal = lapse_proposal(name, agent.manifest.lifecycle, diagnosis)
        if proposal is not None:
            created = await self._proposals.create(proposal)
            suffix = " confirm-safety" if created.touches_safety else ""
            lines.append(
                f"I have a proposal ({created.id}): {created.summary}. Say "
                f"'approve {created.id}{suffix}' if that lands, or "
                f"'reject {created.id}'."
            )
        await self._send_scheduled(channel, "\n".join(lines))

    @staticmethod
    def _render_diagnosis(name: str, diagnosis: LapseDiagnosis) -> str:
        cause_text = {
            "too_big": "the habit may be too big right now",
            "bad_cue": "the cue it is anchored to is not firing",
            "life_event": "life got in the way, which is fair",
            "wrong_goal": "this may not be a goal you actually want",
            "unknown": "I am not sure why yet",
        }.get(diagnosis.cause, diagnosis.cause)
        lines = [
            f"Checking in on {name}. A couple of misses is data, not a "
            "failure, and none of it is held against you.",
            f"My read: {cause_text}.",
        ]
        if diagnosis.detail:
            lines.append(diagnosis.detail)
        return "\n".join(lines)

    # --- commands ---------------------------------------------------------

    async def _try_command(self, message: InboundMessage, text: str) -> bool:
        """Handle owner commands; True when the message was consumed."""
        lowered = text.lower()
        parts = text.split()
        head = parts[0].lower() if parts else ""
        channel = message.channel

        if lowered == _STOP_PHRASE:
            self.stop_requested = True
            await self._send(
                channel, "Understood, shutting down now. Start me again whenever."
            )
            return True
        if lowered == "help":
            await self._send(channel, _HELP_TEXT)
            return True
        if lowered == "status":
            await self._send(channel, await self._render_status())
            return True
        if lowered == "agents":
            await self._send(channel, self._render_agents())
            return True
        if lowered == "proposals":
            await self._send(channel, await self._render_proposals())
            return True
        if lowered == "pending":
            await self._send(channel, await self._render_pending())
            return True
        if lowered == "reflect":
            reflection = await self._reflection.reflect(
                self._registry, self._config.heartbeat.reflection_days
            )
            await self._send(channel, self._render_reflection(reflection))
            return True
        # Governance heads are intercepted even at the wrong arity: a
        # mistyped 'confirm' or 'approve <id> confirm safety' silently
        # feeding an open builder session (or the generic fallback) is how
        # a gated action quietly never runs. Only these heads are reserved;
        # remember/recall/forget stay loose because 'forget it' is a
        # builder cancel phrase and multi-word forms are natural language.
        if head == "confirm":
            if len(parts) == 2:
                await self._confirm_action(channel, parts[1])
            else:
                await self._send(
                    channel,
                    "Usage: confirm <id>. Say 'pending' to see the ids "
                    "waiting on you.",
                )
            return True
        if head == "deny":
            if len(parts) == 2:
                await self._deny_action(channel, parts[1])
            else:
                await self._send(
                    channel,
                    "Usage: deny <id>. Say 'pending' to see the ids waiting on you.",
                )
            return True
        if head == "approve":
            tail = " ".join(parts[2:]).lower()
            # The space variant is the typo the safety prompt itself
            # invites; accepting it keeps the deliberate two-word phrase
            # while removing the trap.
            confirm_safety = tail in ("confirm-safety", "confirm safety")
            if len(parts) >= 2 and (not tail or confirm_safety):
                await self._approve_proposal(channel, parts[1], confirm_safety)
            else:
                await self._send(
                    channel,
                    "Usage: approve <id>, or approve <id> confirm-safety for "
                    "a proposal that touches safety settings.",
                )
            return True
        if head == "reject":
            if len(parts) == 2:
                await self._reject_proposal(channel, parts[1])
            else:
                await self._send(
                    channel,
                    "Usage: reject <id>. Say 'proposals' to see what is pending.",
                )
            return True
        if lowered == "trust":
            await self._send(channel, await self._render_trust())
            return True
        if head == "distrust":
            # Reserved at any arity for the same reason as confirm/deny: a
            # mistyped revocation must never leak into agent routing.
            if len(parts) == 3:
                await self._trust.reset(parts[1], parts[2], cause="owner distrust")
                await self._send(
                    channel,
                    f"Done: {parts[1]} calling {parts[2]} starts from zero "
                    "and previews again.",
                )
            else:
                await self._send(
                    channel,
                    "Usage: distrust <agent> <tool>. Say 'trust' to see the "
                    "workflows on record.",
                )
            return True

        if head == "remember" and len(parts) >= 2:
            fact = text.split(maxsplit=1)[1].strip()
            memory = await self._memory.remember(fact, source="owner")
            await self._send(
                channel,
                f"Filed ({memory.id}). I will bring it up whenever it matters; "
                f"say 'forget {memory.id}' to delete it.",
            )
            return True
        recall_query = self._recall_query(text, lowered, parts)
        if recall_query is not None:
            await self._send(channel, await self._render_recall(recall_query))
            return True
        if lowered == "memories":
            await self._send(channel, await self._render_memories())
            return True
        if head == "forget" and len(parts) == 2:
            if await self._memory.forget(parts[1]):
                await self._send(channel, f"Forgotten: {parts[1]}. Gone from the record.")
            else:
                await self._send(channel, f"I have no memory with id {parts[1]}.")
            return True

        # Roadmap to a goal: the headline move. One live path, one next small
        # win, advanced a step at a time. 'win'/'won' alone closes the active
        # step; 'win <text>' logs a side win without advancing the path.
        if head == "goal" and len(parts) >= 2:
            goal_text = text.split(maxsplit=1)[1].strip()
            context = await self._memory.context_for(goal_text)
            roadmap = await self._roadmap.set_goal(goal_text, context=context)
            await self._send(channel, self._render_new_roadmap(roadmap))
            return True
        if lowered == "goal":
            await self._send(
                channel,
                "Usage: goal <something you want>. I will lay a path of "
                "small wins toward it.",
            )
            return True
        if lowered == "roadmap":
            await self._send(channel, await self._render_roadmap())
            return True
        if lowered in ("next", "next win"):
            await self._send(channel, await self._render_next_win())
            return True
        if lowered in ("win", "won"):
            await self._send(channel, await self._complete_next_win())
            return True
        if head in ("win", "won") and len(parts) >= 2:
            win = await self._roadmap.record_win(text.split(maxsplit=1)[1].strip())
            await self._send(
                channel,
                f"Logged that win: {win.text}. Momentum is what counts here, "
                "not streaks.",
            )
            return True
        if lowered == "wins":
            await self._send(channel, await self._render_wins())
            return True

        goal = ""
        if lowered.startswith("new agent "):
            goal = text[len("new agent") :].strip()
        elif head in ("optimise", "optimize") and len(parts) >= 2:
            goal = text.split(maxsplit=1)[1].strip()
        if goal:
            # A fresh build supersedes any open session; otherwise the old
            # one resurrects later and intercepts unrelated messages.
            superseded = await self._builder.abandon_active()
            _, first_message = await self._builder.start(goal, self._registry)
            if superseded is not None:
                first_message = (
                    "(I closed the building session that was still open.)\n"
                    + first_message
                )
            await self._send(channel, first_message)
            return True

        return False

    @staticmethod
    def _recall_query(text: str, lowered: str, parts: list[str]) -> str | None:
        """Extract a memory query from command or natural phrasing; None if absent."""
        if parts and parts[0].lower() == "recall" and len(parts) >= 2:
            return text.split(maxsplit=1)[1].strip()
        for prefix in (
            "what do you know about ",
            "what did i say about ",
            "what did i tell you about ",
        ):
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip().rstrip("?")
        return None

    async def _render_recall(self, query: str) -> str:
        memories = await self._memory.recall(query, limit=5)
        if not memories:
            return (
                f"Nothing filed about '{query}' yet. Say 'remember <fact>' "
                "and I will keep it."
            )
        lines = [f"What I have about '{query}':"]
        for memory in memories:
            when = f" ({memory.at.date().isoformat()})" if memory.at else ""
            lines.append(f"- {memory.text}{when} [{memory.id}]")
        return "\n".join(lines)

    async def _render_memories(self) -> str:
        memories = await self._memory.recent(limit=10)
        if not memories:
            return "No memories filed yet. Say 'remember <fact>' to start."
        lines = ["Recent memories (newest first):"]
        for memory in memories:
            when = f" ({memory.at.date().isoformat()})" if memory.at else ""
            lines.append(f"- [{memory.kind}] {memory.text}{when} [{memory.id}]")
        lines.append("Say 'forget <id>' to delete one.")
        return "\n".join(lines)

    # --- roadmap rendering ------------------------------------------------

    @staticmethod
    def _milestone_lines(
        milestone: Milestone, *, header: str = "Your next small win"
    ) -> list[str]:
        """The next-step block: title plus whichever of why/when/done are set."""
        lines = [f"{header}: {milestone.title}"]
        if milestone.why:
            lines.append(f"  why: {milestone.why}")
        if milestone.anchor:
            lines.append(f"  when: {milestone.anchor}")
        if milestone.done_signal:
            lines.append(f"  done when: {milestone.done_signal}")
        return lines

    def _render_new_roadmap(self, roadmap: Roadmap) -> str:
        nxt = roadmap.next_win
        if nxt is None:
            # No model, or nothing to decompose: be honest, never fake a path.
            return (
                f"Goal set: {roadmap.goal}. I could not lay out steps for it "
                "right now (no model connected, or nothing to break down). Try "
                "again with a model running."
            )
        lines = [
            f"Here is the path to '{roadmap.goal}', laid out as "
            f"{len(roadmap.milestones)} small wins, each almost too small to "
            "fail. We take them one at a time.",
            "",
        ]
        lines.extend(self._milestone_lines(nxt))
        lines.append("Say 'win' when it is done, or 'roadmap' for the whole path.")
        return "\n".join(lines)

    @staticmethod
    def _empty_path_text(goal: str) -> str:
        """When a stored roadmap has no steps (a model could not decompose)."""
        return (
            f"Goal set: {goal}, but it has no steps yet. Say 'goal {goal}' "
            "again with a model connected and I will break it into small wins."
        )

    async def _render_roadmap(self) -> str:
        roadmap = await self._roadmap.current()
        if roadmap is None:
            return (
                "No goal set yet. Say 'goal <something you want>' and I will lay "
                "a path of small wins to it."
            )
        if not roadmap.milestones:
            return self._empty_path_text(roadmap.goal)
        lines = [
            f"Goal: {roadmap.goal}",
            f"Progress: {roadmap.won_count} of {len(roadmap.milestones)} wins.",
        ]
        nxt = roadmap.next_win
        if nxt is None:
            lines.append(
                "Every step is done. Say 'goal <new goal>' when you are ready "
                "for the next one."
            )
            return "\n".join(lines)
        lines.append("")
        lines.extend(self._milestone_lines(nxt))
        later = [
            m.title
            for m in roadmap.milestones
            if m.status == "pending" and m.id != nxt.id
        ]
        if later:
            lines.append("Later, once that lands:")
            lines.extend(f"  - {title}" for title in later)
        lines.append("Say 'win' when the next one is done.")
        return "\n".join(lines)

    async def _render_next_win(self) -> str:
        roadmap = await self._roadmap.current()
        if roadmap is None:
            return (
                "No goal set yet. Say 'goal <something you want>' to lay a path "
                "of small wins."
            )
        if not roadmap.milestones:
            return self._empty_path_text(roadmap.goal)
        nxt = roadmap.next_win
        if nxt is None:
            return (
                f"Nothing left on the path to '{roadmap.goal}': every win is in. "
                "Say 'goal <new goal>' for the next one."
            )
        return "\n".join(self._milestone_lines(nxt))

    async def _complete_next_win(self) -> str:
        roadmap, won, new_next = await self._roadmap.complete_next()
        if roadmap is None:
            return (
                "No goal set yet, so there is no step to mark. Say 'goal "
                "<something you want>' to start a path."
            )
        if not roadmap.milestones:
            return self._empty_path_text(roadmap.goal)
        if won is None:
            return (
                f"Every step toward '{roadmap.goal}' is already done. Say 'goal "
                "<new goal>' for the next one."
            )
        lines = [f"That is a win: {won.title}. Logged, and it counts."]
        if new_next is None:
            lines.append(
                f"That was the last step toward '{roadmap.goal}'. Goal reached. "
                "Say 'goal <new goal>' when you want the next mountain."
            )
        else:
            lines.append("")
            lines.extend(self._milestone_lines(new_next))
        return "\n".join(lines)

    async def _render_wins(self) -> str:
        wins = await self._roadmap.recent_wins(limit=10)
        if not wins:
            return (
                "No wins logged yet. Say 'win' when you finish your next small "
                "step, or 'win <text>' to log one now."
            )
        lines = ["Recent wins (newest first):"]
        for win in wins:
            when = f" ({win.at.date().isoformat()})" if win.at else ""
            lines.append(f"- {win.text}{when}")
        lines.append("Momentum, not streaks. Every one counts.")
        return "\n".join(lines)

    async def _nudge_roadmap(self, channel: str | None) -> None:
        """Surface the one next small win, gently. Never a nag, never a streak.

        Sends nothing when there is no active roadmap or nothing left to win,
        so the daily cadence cannot become noise. Quiet hours are already
        enforced by the heartbeat before this runs.
        """
        roadmap = await self._roadmap.current()
        if roadmap is None or roadmap.next_win is None:
            return
        lines = [f"A gentle nudge on '{roadmap.goal}'. No rush, no streak."]
        lines.extend(self._milestone_lines(roadmap.next_win))
        lines.append("Reply 'win' when it is done.")
        await self._send_scheduled(channel, "\n".join(lines))

    async def _confirm_action(self, channel: str, action_id: str) -> None:
        action = await self._pending.get(action_id)
        if action is None:
            # Not a single action: the id may name a composed intent.
            members = await self._pending.bundle_members(action_id)
            if members:
                await self._confirm_bundle(channel, action_id, members)
                return
            await self._send(channel, f"I have no pending action with id {action_id}.")
            return
        agent = self._registry.get(action.agent)
        try:
            result = await self._dispatcher.execute_confirmed(action_id, agent)
        except AlfredError as exc:
            await self._send(channel, f"Could not execute {action_id}: {exc}")
            return
        if result.ok:
            text = (
                f"Confirmed {action_id}: {action.call.tool} executed. "
                f"Result: {result.model_dump_json()}"
            )
            note = await self._note_trust_approval(action)
            if note:
                text += f"\n{note}"
            await self._send(channel, text)
        else:
            await self._send(
                channel,
                f"Confirmed {action_id}, but {action.call.tool} failed: {result.error}",
            )

    async def _confirm_bundle(
        self, channel: str, bundle_id: str, members: list[PendingAction]
    ) -> None:
        """Execute a composed intent's steps in emission order.

        One confirm covers every member, but honesty rules the failure
        path: the first step that fails stops the chain, and whatever has
        not run stays pending so the owner rules on it with the fault in
        view instead of it executing into a half-broken state.
        """
        lines: list[str] = []
        for step, member in enumerate(members, start=1):
            agent = self._registry.get(member.agent)
            try:
                result = await self._dispatcher.execute_confirmed(member.id, agent)
            except AlfredError as exc:
                lines.append(f"{step}. {member.call.tool}: could not execute ({exc})")
                remaining = members[step:]
                if remaining:
                    ids = ", ".join(m.id for m in remaining)
                    lines.append(
                        f"Stopped there; still pending and untouched: {ids}. "
                        "Rule on them individually once this is sorted."
                    )
                break
            if result.ok:
                lines.append(f"{step}. {member.call.tool}: done")
                note = await self._note_trust_approval(member)
                if note:
                    lines.append(note)
                continue
            lines.append(f"{step}. {member.call.tool}: failed ({result.error})")
            remaining = members[step:]
            if remaining:
                ids = ", ".join(m.id for m in remaining)
                lines.append(
                    f"Stopped there; still pending and untouched: {ids}. "
                    "Rule on them individually once this is sorted."
                )
            break
        else:
            lines.append(f"All {len(members)} steps of intent {bundle_id} ran.")
        await self._send(
            channel, f"Confirmed intent {bundle_id}:\n" + "\n".join(lines)
        )

    async def _deny_action(self, channel: str, action_id: str) -> None:
        # A composed intent denies as one thing, exactly as it confirmed.
        members = await self._pending.bundle_members(action_id)
        if members:
            for member in members:
                await self._pending.resolve(member.id, approved=False)
                await self._trust.reset(
                    member.agent, member.call.tool, cause="denied"
                )
            tools = ", ".join(m.call.tool for m in members)
            await self._send(
                channel,
                f"Denied intent {action_id}: none of it will run ({tools}).",
            )
            return
        try:
            action = await self._pending.resolve(action_id, approved=False)
        except AlfredError as exc:
            await self._send(channel, f"Could not deny {action_id}: {exc}")
            return
        # Any deny zeroes the pair's run toward the autonomy dial: distrust
        # is always cheaper than trust.
        await self._trust.reset(action.agent, action.call.tool, cause="denied")
        await self._send(
            channel,
            f"Denied {action_id}: {action.call.tool} will not run.",
        )

    async def _note_trust_approval(self, action: PendingAction) -> str | None:
        """Count one confirm toward the dial; a line only when it matters.

        Only previewed cross-system reversible writes feed trust: a
        destructive confirm can never relax anything, and external content
        never gets counted at all.
        """
        if (
            action.tier != CapabilityTier.REVERSIBLE_WRITE
            or not action.cross_system
            or action.provenance == "external"
        ):
            return None
        approvals, newly = await self._trust.record_approval(
            action.agent, action.call.tool
        )
        if not newly:
            return None
        return (
            f"That is {approvals} approvals in a row for {action.agent} "
            f"calling {action.call.tool}, so I will stop previewing this "
            f"workflow. Say 'distrust {action.agent} {action.call.tool}' any "
            "time to bring the preview back."
        )

    async def _render_trust(self) -> str:
        threshold = self._trust.threshold
        lines: list[str] = []
        if threshold <= 0:
            lines.append(
                "The autonomy dial is off: every cross-system write previews, "
                "always. Set policy.trust_after_approvals in config/alfred.yaml "
                "to let a workflow earn its way out of the preview."
            )
        else:
            lines.append(
                f"The autonomy dial is set to {threshold}: a workflow you "
                f"approve {threshold} times in a row stops previewing. A deny "
                "resets it; 'distrust <agent> <tool>' revokes it."
            )
        live = [r for r in await self._trust.all_records() if r.approvals > 0]
        if not live:
            lines.append("No workflow has approvals on record yet.")
            return "\n".join(lines)
        for record in live:
            if threshold > 0 and record.approvals >= threshold:
                lines.append(
                    f"- {record.agent} calling {record.tool}: trusted "
                    f"({record.approvals} approvals); previews skipped"
                )
            else:
                target = f" of {threshold}" if threshold > 0 else ""
                lines.append(
                    f"- {record.agent} calling {record.tool}: "
                    f"{record.approvals}{target} approvals"
                )
        return "\n".join(lines)

    async def _approve_proposal(
        self, channel: str, proposal_id: str, confirm_safety: bool
    ) -> None:
        pending = await self._proposals.list_pending()
        proposal = next((p for p in pending if p.id == proposal_id), None)
        if proposal is None:
            await self._send(
                channel, f"I have no pending proposal with id {proposal_id}."
            )
            return
        if proposal.touches_safety and not confirm_safety:
            await self._send(
                channel,
                f"Proposal {proposal_id} touches safety settings (allowlists or "
                "permissions), so a plain approve is not enough. If you have "
                f"read it and still want it, say: approve {proposal_id} "
                "confirm-safety",
            )
            return
        resolved = await self._proposals.resolve(proposal_id, approved=True)
        note = await self._apply_proposal(resolved)
        await self._send(
            channel, f"Approved proposal {proposal_id}: {resolved.summary}. {note}"
        )

    async def _reject_proposal(self, channel: str, proposal_id: str) -> None:
        try:
            await self._proposals.resolve(proposal_id, approved=False)
        except AlfredError as exc:
            await self._send(channel, f"Could not reject {proposal_id}: {exc}")
            return
        await self._send(channel, f"Rejected proposal {proposal_id}; nothing changes.")

    async def _apply_proposal(self, proposal: Proposal) -> str:
        """Apply an approved proposal where the runtime knows how; else defer.

        Every application captures the value it replaced back onto the
        stored proposal (so an approved change is reversible by hand from
        the record) and writes a proposal_applied audit event.
        """
        by_hand = (
            "I cannot apply this change automatically; it is approved but "
            "must be applied by hand."
        )
        if proposal.kind is ProposalKind.NEW_AGENT:
            if not proposal.new:
                return by_hand
            try:
                blueprint = AgentBlueprint.model_validate_json(proposal.new)
            except ValueError:
                logger.warning("approved NEW_AGENT proposal %s has an unparseable blueprint", proposal.id)
                return by_hand
            stripped: list[str] = []
            if blueprint.manifest.allowed_tools and not proposal.touches_safety:
                # Least privilege is load-bearing: a proposal that was not
                # flagged (and double-confirmed) as touching safety cannot
                # smuggle in a pre-populated allowlist.
                stripped = list(blueprint.manifest.allowed_tools)
                blueprint.manifest.allowed_tools = []
                logger.warning(
                    "NEW_AGENT proposal %s carried tools %s without "
                    "touches_safety; stripped",
                    proposal.id,
                    stripped,
                )
            try:
                path = materialise_agent(self._agents_dir, blueprint)
            except (AlfredError, OSError) as exc:
                # OSError too: a locked file or full disk must strand the
                # approval honestly, never escape as a generic crash.
                return f"Approved, but I could not write the agent folder: {exc}"
            self._registry.add(
                LoadedAgent(
                    manifest=blueprint.manifest,
                    prompt=blueprint.prompt_md,
                    path=str(path),
                )
            )
            await self._record_application(proposal, old=None)
            note = f"Agent '{blueprint.manifest.name}' is created and live."
            if stripped:
                note += (
                    f" Its proposed tools ({', '.join(stripped)}) were NOT "
                    "granted: tool access needs a touches-safety proposal or "
                    "a manifest edit by you."
                )
            return note

        if proposal.kind is ProposalKind.LIFECYCLE_CHANGE:
            agent = self._registry.get(proposal.agent)
            if agent is None or not proposal.new:
                return by_hand
            try:
                new_state = Lifecycle(proposal.new)
            except ValueError:
                return by_hand
            old_state = agent.manifest.lifecycle.value
            agent.manifest.lifecycle = new_state
            on_disk = self._write_manifest(agent)
            if on_disk == "failed":
                # Memory must match disk: a half-applied lifecycle would
                # silently revert on the next restart, so undo and say so.
                agent.manifest.lifecycle = Lifecycle(old_state)
                return (
                    "Approved, but I could not write the manifest to disk; "
                    "nothing changed. The approval is on record for applying "
                    "by hand."
                )
            await self._record_application(proposal, old=old_state)
            return (
                f"'{proposal.agent}' is now {new_state.value} (was {old_state})"
                + (
                    "."
                    if on_disk == "ok"
                    else " (in memory only; its folder is missing on disk)."
                )
            )

        if proposal.kind is ProposalKind.PROMPT_CHANGE:
            agent = self._registry.get(proposal.agent)
            if agent is None or proposal.new is None:
                return by_hand
            old_prompt = agent.prompt
            folder = self._agent_folder(agent)
            if folder.is_dir():
                try:
                    # Disk before memory: a failed write must leave the old
                    # prompt live everywhere, or a restart silently reverts
                    # an approval the owner believes landed.
                    (folder / "agent.md").write_text(proposal.new, encoding="utf-8")
                except OSError as exc:
                    return (
                        f"Approved, but I could not write agent.md ({exc}); "
                        "nothing changed. The approval is on record for "
                        "applying by hand."
                    )
                agent.prompt = proposal.new
                await self._record_application(proposal, old=old_prompt)
                return (
                    f"'{proposal.agent}' has its new prompt. The previous one "
                    "is kept on the proposal record."
                )
            agent.prompt = proposal.new
            await self._record_application(proposal, old=old_prompt)
            return (
                f"'{proposal.agent}' has its new prompt in memory only; "
                "its folder is missing on disk."
            )

        if proposal.kind is ProposalKind.RETIRE_AGENT:
            agent = self._registry.get(proposal.agent)
            if agent is None:
                return by_hand
            old_state = agent.manifest.lifecycle.value
            agent.manifest.lifecycle = Lifecycle.RETIRED
            on_disk = self._write_manifest(agent)
            if on_disk == "failed":
                agent.manifest.lifecycle = Lifecycle(old_state)
                return (
                    "Approved, but I could not write the manifest to disk; "
                    "nothing changed. The approval is on record for applying "
                    "by hand."
                )
            await self._record_application(proposal, old=old_state)
            return (
                f"'{proposal.agent}' is retired, with thanks for the data it gave"
                + (
                    "."
                    if on_disk == "ok"
                    else " (in memory only; its folder is missing on disk)."
                )
            )

        return by_hand

    async def _record_application(self, proposal: Proposal, old: str | None) -> None:
        """Persist the replaced value onto the proposal and audit the apply."""
        if old is not None and proposal.old is None:
            updated = proposal.model_copy(update={"old": old})
            await self._store.put(
                Collections.PROPOSALS, updated.id, updated.model_dump(mode="json")
            )
        await audit(
            self._store,
            self._clock,
            "proposal_applied",
            proposal_id=proposal.id,
            kind=proposal.kind.value,
            agent=proposal.agent,
            touches_safety=proposal.touches_safety,
        )

    def _agent_folder(self, agent: LoadedAgent) -> Path:
        return Path(agent.path) if agent.path else self._agents_dir / agent.manifest.name

    def _write_manifest(self, agent: LoadedAgent) -> str:
        """Persist the manifest; returns "ok", "missing", or "failed".

        "missing" (no folder) keeps the in-memory change: a folderless
        agent is a known degraded mode. "failed" (write error) tells the
        caller to revert, because memory diverging from an EXISTING folder
        silently undoes the change on the next restart.
        """
        folder = self._agent_folder(agent)
        if not folder.is_dir():
            logger.warning(
                "agent folder missing on disk, manifest change kept in memory: %s",
                folder,
            )
            return "missing"
        try:
            (folder / "manifest.yaml").write_text(
                render_manifest_yaml(agent.manifest), encoding="utf-8"
            )
        except OSError:
            logger.exception(
                "failed to write manifest for %s", agent.manifest.name
            )
            return "failed"
        return "ok"

    # --- builder continuation ---------------------------------------------

    async def _try_builder_continuation(
        self, message: InboundMessage, text: str
    ) -> bool:
        session = await self._builder.active_session()
        if session is None:
            return False
        updated, reply = await self._builder.step(session.id, text, self._registry)
        await self._send(message.channel, reply)
        if updated.stage is BuilderStage.DONE and updated.blueprint is not None:
            blueprint = updated.blueprint
            try:
                path = materialise_agent(self._agents_dir, blueprint)
            except (AlfredError, OSError) as exc:
                # The approval marked the session DONE before this write;
                # reopen it so the elicited lever and blueprint survive and
                # the owner can rename or retry instead of starting over.
                await self._builder.reopen(updated.id)
                await self._send(
                    message.channel,
                    f"I could not write the agent folder: {exc}. The build "
                    "is still open: rename it (e.g. 'call it something-else') "
                    "and approve again, or say 'cancel' to drop it.",
                )
                return True
            self._registry.add(
                LoadedAgent(
                    manifest=blueprint.manifest,
                    prompt=blueprint.prompt_md,
                    path=str(path),
                )
            )
            await self._send(
                message.channel,
                f"Agent '{blueprint.manifest.name}' is created at {path} and live.",
            )
        return True

    # --- routing ------------------------------------------------------------

    async def _route_and_run(self, message: InboundMessage, text: str) -> None:
        routed = route(message, self._registry)

        # Outcome shorthand is recorded before the agent runs so the run
        # reacts with the outcome already in the record. Guarded hard:
        # owner only, exactly one routed agent, and a SHORT message, so an
        # ordinary sentence that happens to contain "done" cannot silently
        # corrupt adherence stats.
        if (
            message.provenance == "owner"
            and len(routed) == 1
            and len(text.split()) <= 8
        ):
            status = parse_outcome_report(text)
            if status is not None:
                name = routed[0].manifest.name
                latest = await self._latest_plan(name)
                outcome = Outcome(
                    agent=name,
                    status=status,
                    report=text,
                    plan_id=latest.id if latest else None,
                    item_id=None,
                )
                await self._user_model.record_outcome(outcome)
                await self._send(
                    message.channel,
                    f"Logged outcome '{status.value}' for {name}.",
                )

        if not routed:
            await self._send(message.channel, self._fallback_text())
            return

        plans: list[Plan] = []
        for agent in routed:
            result = await self._executor.run(
                agent, text=text, provenance=message.provenance
            )
            await self._deliver_result(message.channel, result)
            if result.plan is not None:
                plans.append(result.plan)

        if len(plans) >= 2:
            profile = await self._user_model.get_profile()
            schedule = await self._conductor.reconcile(plans, profile)
            await self._send(message.channel, self._render_schedule(schedule))
            # Schedules live in their own collection: a ReconciledSchedule
            # doc inside PLANS would pollute every Plan query.
            await self._store.append(
                Collections.SCHEDULES, schedule.model_dump(mode="json")
            )

    async def _deliver_result(self, channel: str, result: ExecutionResult) -> None:
        for reply in result.replies:
            await self._send(channel, reply)
        if result.pending:
            await self._send(channel, self._render_gated(result.pending))

    @staticmethod
    def _render_gated(pending: list[PendingAction]) -> str:
        """The confirmation ask for a run's gated actions.

        Two or more actions sharing a bundle are one composed intent: they
        are presented as numbered steps under the intent id the owner can
        rule on once. Anything else renders as the familiar single lines.
        """
        bundles: dict[str, list[PendingAction]] = {}
        for action in pending:
            if action.bundle_id is not None:
                bundles.setdefault(action.bundle_id, []).append(action)
        composed = {
            bid: sorted(members, key=lambda a: a.bundle_seq)
            for bid, members in bundles.items()
            if len(members) > 1
        }
        lines: list[str] = []
        for bid, members in composed.items():
            lines.append(
                f"This needs {len(members)} actions from {members[0].agent}, "
                f"previewed together as one intent ({bid}):"
            )
            for step, action in enumerate(members, start=1):
                reason = action.reason or "no reason given"
                lines.append(
                    f"  {step}. {action.call.tool} ({reason}) [{action.id}]"
                )
            lines.append(
                f"Say 'confirm {bid}' to run all {len(members)} in order, "
                f"'deny {bid}' to reject them all, or rule on single ids."
            )
        singles = [
            action
            for action in pending
            if action.bundle_id not in composed
        ]
        if singles:
            lines.append("These actions need your confirmation before they run:")
            for action in singles:
                reason = action.reason or "no reason given"
                lines.append(
                    f"- {action.id}: {action.call.tool} by {action.agent} ({reason})"
                )
            lines.append(
                "Say 'confirm <id>' to execute one, or 'deny <id>' to reject it."
            )
        return "\n".join(lines)

    async def _latest_plan(self, agent_name: str) -> Plan | None:
        # Newest window only: append keys are chronological, so the latest
        # plan lives in the most recent rows and a year of history must not
        # be decoded on every short outcome message.
        docs = await self._store.query(
            Collections.PLANS,
            where={"agent": agent_name},
            limit=50,
            newest_first=True,
        )
        plans: list[Plan] = []
        for doc in docs:
            data = {k: v for k, v in doc.items() if k != "_key"}
            try:
                plans.append(Plan.model_validate(data))
            except ValueError:
                continue
        if not plans:
            return None
        # Pair the timestamp with the plan rather than reaching back through
        # a sentinel: datetime.min is naive, and comparing it against an
        # aware created_at would raise rather than sort.
        dated = [(p.created_at, p) for p in plans if p.created_at is not None]
        if dated:
            return max(dated, key=lambda pair: pair[0])[1]
        return plans[0]

    # --- rendering ------------------------------------------------------------

    def _fallback_text(self) -> str:
        lines = [
            "I am ALFRED, your self-hosted life-optimization system. I plan "
            "and coordinate your goals across domains and hold the plans "
            "accountable over time; I am not a general chatbot.",
        ]
        active = self._registry.active()
        if active:
            lines.append("No agent claimed that message. Active agents and their trigger words:")
            for agent in active:
                triggers = agent.manifest.triggers
                words = ", ".join(triggers.keywords) if triggers.keywords else (
                    "always on" if triggers.always else "none"
                )
                lines.append(
                    f"- {agent.manifest.name} ({agent.manifest.lifecycle.value}): {words}"
                )
        else:
            lines.append(
                "No agents are active yet. Say 'new agent <goal>' to build the first one."
            )
        lines.append(
            "Commands: help, status, goal <goal>, roadmap, win, agents, "
            "proposals, confirm <id>, deny <id>, approve <id>, reject <id>, "
            "reflect, new agent <goal>, alfred stop."
        )
        return "\n".join(lines)

    async def _render_status(self) -> str:
        profile = await self._user_model.get_profile()
        actions = await self._pending.list_pending()
        proposals = await self._proposals.list_pending()
        lines = ["ALFRED status."]
        active = self._registry.active()
        if active:
            lines.append("Active agents:")
            for agent in active:
                name = agent.manifest.name
                stats = profile.adherence.get(name, AdherenceStats())
                lines.append(
                    f"- {name} ({agent.manifest.lifecycle.value}): "
                    f"adherence {adherence_signal(stats)}"
                )
        else:
            lines.append("No active agents.")
        if self._skipped_agents:
            names = ", ".join(name for name, _ in self._skipped_agents)
            lines.append(
                f"Not loaded (folder failed to parse): {names}. "
                "Say 'agents' for the reasons."
            )
        roadmap = await self._roadmap.current()
        if roadmap is not None and roadmap.next_win is not None:
            lines.append(
                f"Goal '{roadmap.goal}': {roadmap.won_count} of "
                f"{len(roadmap.milestones)} wins. Next: {roadmap.next_win.title}."
            )
        lines.append(
            f"Pending actions: {len(actions)}. Pending proposals: {len(proposals)}."
        )
        if actions:
            lines.append("Say 'pending' to list the actions waiting on you.")
        return "\n".join(lines)

    def _render_agents(self) -> str:
        agents = self._registry.all()
        if not agents and not self._skipped_agents:
            return "No agents loaded. Say 'new agent <goal>' to build one."
        lines = ["Known agents:"] if agents else []
        for agent in agents:
            lines.append(
                f"- {agent.manifest.name} ({agent.manifest.lifecycle.value}): "
                f"{agent.manifest.description}"
            )
        for name, reason in self._skipped_agents:
            lines.append(f"- {name}: NOT LOADED ({reason})")
        return "\n".join(lines)

    async def _render_pending(self) -> str:
        # Rendered via list_pending() on purpose: it expires stale actions
        # on read, so nothing expired can resurface as confirmable. Shares
        # _render_gated so composed intents group here exactly as they did
        # when first surfaced.
        actions = await self._pending.list_pending()
        if not actions:
            return "No pending actions."
        return self._render_gated(actions)

    async def _render_proposals(self) -> str:
        pending = await self._proposals.list_pending()
        if not pending:
            return "No pending proposals."
        lines = ["Pending proposals:"]
        for proposal in pending:
            line = (
                f"- {proposal.id} [{proposal.kind.value}] {proposal.agent}: "
                f"{proposal.summary}"
            )
            if proposal.touches_safety:
                line += (
                    f" (touches safety: needs 'approve {proposal.id} confirm-safety')"
                )
            lines.append(line)
        lines.append("Say 'approve <id>' or 'reject <id>'.")
        return "\n".join(lines)

    def _render_reflection(self, reflection: Reflection) -> str:
        lines = [f"Reflection over the last {reflection.window_days} days."]
        if reflection.insights:
            lines.append("Insights:")
            lines.extend(f"- {insight}" for insight in reflection.insights)
        else:
            lines.append("No insights this round.")
        if reflection.proposals:
            lines.append("Proposals awaiting your approval:")
            for proposal in reflection.proposals:
                lines.append(
                    f"- {proposal.id} [{proposal.kind.value}] {proposal.agent}: "
                    f"{proposal.summary}"
                )
            lines.append("Say 'proposals' to review them.")
        return "\n".join(lines)

    def _render_schedule(self, schedule: ReconciledSchedule) -> str:
        lines = [schedule.summary or "Plans reconciled."]
        if schedule.adjustments:
            lines.append("Adjustments:")
            for adj in schedule.adjustments:
                target = f" {adj.item_id}" if adj.item_id else ""
                lines.append(f"- {adj.agent}: {adj.action}{target}: {adj.detail}")
        for warning in schedule.warnings:
            lines.append(f"Warning: {warning}")
        lines.append(f"Total load: {schedule.total_load}.")
        return "\n".join(lines)

    # --- plumbing ----------------------------------------------------------

    async def _send(self, channel: str, text: str) -> None:
        await self._transport.send(OutboundMessage(channel=channel, text=text))

    def _scheduled_channel(self) -> str | None:
        """Where proactive output goes: configured channel, else the channel
        the owner last spoke on this process, else nowhere (loudly).

        Deliberately no fallback to the stored message log: a "cli" channel
        from yesterday's terminal session must not capture Discord-mode
        sends into a transport that cannot deliver them.
        """
        if self._config.discord.channel_id:
            # Duck-typed on purpose: TransportPort is a frozen surface, so
            # route-awareness stays optional. When the transport can say
            # the Discord route is absent (token unset, adapter off), a
            # stale channel_id in config must not blackhole every check-in
            # while the owner chats happily on Telegram.
            has_route = getattr(self._transport, "has_route", None)
            if has_route is None or has_route("discord"):
                return f"discord:{self._config.discord.channel_id}"
            logger.warning(
                "discord.channel_id is set but the Discord transport is not "
                "running; falling back to the owner's latest channel"
            )
        return self._last_channel

    async def _send_scheduled(self, channel: str | None, text: str) -> None:
        if channel is None:
            logger.warning(
                "scheduled output has no destination (no discord.channel_id "
                "configured and the owner has not messaged yet); dropping: %.120s",
                text,
            )
            return
        await self._send(channel, text)

    async def _latest_plans_for_week(self, week_of: date | None) -> list[Plan]:
        """The newest plan per active agent for the given week."""
        docs = await self._store.query(
            Collections.PLANS, limit=200, newest_first=True
        )
        active = {agent.manifest.name for agent in self._registry.active()}
        by_agent: dict[str, Plan] = {}
        for doc in docs:
            data = {k: v for k, v in doc.items() if k != "_key"}
            try:
                plan = Plan.model_validate(data)
            except ValueError:
                continue
            if plan.agent not in active or plan.week_of != week_of:
                continue
            by_agent.setdefault(plan.agent, plan)  # newest first wins
        return list(by_agent.values())
