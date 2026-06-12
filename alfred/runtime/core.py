"""AlfredCore: the assembled brain.

One inbound message flows through a fixed pipeline: log it, try owner
commands, continue any open builder session, then route to agents. Every
reply leaves through the transport, every failure is logged and surfaced
to the owner as a short honest notice, and nothing here ever widens
access: confirmation and approval flows only call into governance.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from alfred.config import AlfredConfig
from alfred.domain.builder import AgentBuilder
from alfred.domain.conductor import Conductor
from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.executor import AgentExecutor
from alfred.domain.feedback import adherence_signal, parse_outcome_report
from alfred.domain.governance import PendingActions, Proposals, audit
from alfred.domain.reflection import ReflectionEngine
from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.routing import route
from alfred.domain.schemas import (
    AdherenceStats,
    AgentBlueprint,
    BuilderStage,
    Collections,
    ExecutionResult,
    InboundMessage,
    Lifecycle,
    Outcome,
    Plan,
    Proposal,
    ProposalKind,
    ReconciledSchedule,
    Reflection,
    ScheduledTrigger,
)
from alfred.domain.user_model import UserModelService
from alfred.errors import AlfredError
from alfred.ports import ClockPort, OutboundMessage, StorePort, TransportPort
from alfred.runtime.agent_loader import materialise_agent, render_manifest_yaml

logger = logging.getLogger(__name__)

_STOP_PHRASE = "alfred stop"

_HELP_TEXT = "\n".join(
    [
        "ALFRED commands:",
        "- help: this summary",
        "- status: active agents, adherence, pending actions and proposals",
        "- agents: list every known agent",
        "- confirm <id> / deny <id>: rule on a gated tool action",
        "- proposals: list pending self-change proposals",
        "- approve <id> [confirm-safety] / reject <id>: rule on a proposal",
        "- reflect: run a strategy review now",
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
        store: StorePort,
        clock: ClockPort,
        transport: TransportPort,
        config: AlfredConfig,
        agents_dir: Path,
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
        self._store = store
        self._clock = clock
        self._transport = transport
        self._config = config
        self._agents_dir = Path(agents_dir)
        self.stop_requested = False
        # Freshest channel the owner spoke on; scheduled output goes there
        # when no channel is configured. In-memory on purpose: a stale "cli"
        # channel from a previous chat session must not capture Discord-mode
        # proactive sends.
        self._last_channel: str | None = None

    # --- entry points ---------------------------------------------------

    async def handle_inbound(self, message: InboundMessage) -> None:
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
                await self._send(
                    message.channel,
                    "Sorry, something went wrong while handling that "
                    f"({type(exc).__name__}). The details are in my log.",
                )
            except Exception:
                logger.exception("failed to deliver the error notice")

    async def run_scheduled(self, trigger: ScheduledTrigger) -> None:
        channel = self._scheduled_channel()
        if trigger.reason == "reflection":
            reflection = await self._reflection.reflect(
                self._registry, self._config.heartbeat.reflection_days
            )
            await self._send_scheduled(channel, self._render_reflection(reflection))
            return

        agent = self._registry.get(trigger.agent)
        if agent is None:
            logger.warning(
                "scheduled trigger for unknown agent %r (reason=%s); skipping",
                trigger.agent,
                trigger.reason,
            )
            return
        text = _CHECK_IN_TEXT if trigger.reason == "check_in" else _PLANNING_TEXT
        result = await self._executor.run(agent, text=text, provenance="scheduler")
        if channel is not None:
            await self._deliver_result(channel, result)

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
        if lowered == "reflect":
            reflection = await self._reflection.reflect(
                self._registry, self._config.heartbeat.reflection_days
            )
            await self._send(channel, self._render_reflection(reflection))
            return True
        if head == "confirm" and len(parts) == 2:
            await self._confirm_action(channel, parts[1])
            return True
        if head == "deny" and len(parts) == 2:
            await self._deny_action(channel, parts[1])
            return True
        if head == "approve" and len(parts) in (2, 3):
            confirm_safety = len(parts) == 3 and parts[2].lower() == "confirm-safety"
            if len(parts) == 3 and not confirm_safety:
                return False  # unknown trailing word; not this command
            await self._approve_proposal(channel, parts[1], confirm_safety)
            return True
        if head == "reject" and len(parts) == 2:
            await self._reject_proposal(channel, parts[1])
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

    async def _confirm_action(self, channel: str, action_id: str) -> None:
        action = await self._pending.get(action_id)
        if action is None:
            await self._send(channel, f"I have no pending action with id {action_id}.")
            return
        agent = self._registry.get(action.agent)
        try:
            result = await self._dispatcher.execute_confirmed(action_id, agent)
        except AlfredError as exc:
            await self._send(channel, f"Could not execute {action_id}: {exc}")
            return
        if result.ok:
            await self._send(
                channel,
                f"Confirmed {action_id}: {action.call.tool} executed. "
                f"Result: {result.model_dump_json()}",
            )
        else:
            await self._send(
                channel,
                f"Confirmed {action_id}, but {action.call.tool} failed: {result.error}",
            )

    async def _deny_action(self, channel: str, action_id: str) -> None:
        try:
            action = await self._pending.resolve(action_id, approved=False)
        except AlfredError as exc:
            await self._send(channel, f"Could not deny {action_id}: {exc}")
            return
        await self._send(
            channel,
            f"Denied {action_id}: {action.call.tool} will not run.",
        )

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
            except AlfredError as exc:
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
            await self._record_application(proposal, old=old_state)
            return (
                f"'{proposal.agent}' is now {new_state.value} (was {old_state})"
                + ("." if on_disk else " (in memory only; its folder is missing on disk).")
            )

        if proposal.kind is ProposalKind.PROMPT_CHANGE:
            agent = self._registry.get(proposal.agent)
            if agent is None or proposal.new is None:
                return by_hand
            old_prompt = agent.prompt
            agent.prompt = proposal.new
            folder = self._agent_folder(agent)
            await self._record_application(proposal, old=old_prompt)
            if folder.is_dir():
                (folder / "agent.md").write_text(proposal.new, encoding="utf-8")
                return (
                    f"'{proposal.agent}' has its new prompt. The previous one "
                    "is kept on the proposal record."
                )
            return (
                f"'{proposal.agent}' has its new prompt in memory only; "
                "its folder is missing on disk."
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

    def _write_manifest(self, agent: LoadedAgent) -> bool:
        folder = self._agent_folder(agent)
        if not folder.is_dir():
            logger.warning(
                "agent folder missing on disk, manifest change kept in memory: %s",
                folder,
            )
            return False
        (folder / "manifest.yaml").write_text(
            render_manifest_yaml(agent.manifest), encoding="utf-8"
        )
        return True

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
            except AlfredError as exc:
                await self._send(
                    message.channel, f"I could not write the agent folder: {exc}"
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
            lines = ["These actions need your confirmation before they run:"]
            for action in result.pending:
                reason = action.reason or "no reason given"
                lines.append(f"- {action.id}: {action.call.tool} ({reason})")
            lines.append("Say 'confirm <id>' to execute one, or 'deny <id>' to reject it.")
            await self._send(channel, "\n".join(lines))

    async def _latest_plan(self, agent_name: str) -> Plan | None:
        docs = await self._store.query(Collections.PLANS, where={"agent": agent_name})
        plans: list[Plan] = []
        for doc in docs:
            data = {k: v for k, v in doc.items() if k != "_key"}
            try:
                plans.append(Plan.model_validate(data))
            except ValueError:
                continue
        if not plans:
            return None
        dated = [p for p in plans if p.created_at is not None]
        if dated:
            return max(dated, key=lambda p: p.created_at or datetime.min)
        return plans[-1]

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
            "Commands: help, status, agents, proposals, confirm <id>, deny <id>, "
            "approve <id>, reject <id>, reflect, new agent <goal>, alfred stop."
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
        lines.append(
            f"Pending actions: {len(actions)}. Pending proposals: {len(proposals)}."
        )
        return "\n".join(lines)

    def _render_agents(self) -> str:
        agents = self._registry.all()
        if not agents:
            return "No agents loaded. Say 'new agent <goal>' to build one."
        lines = ["Known agents:"]
        for agent in agents:
            lines.append(
                f"- {agent.manifest.name} ({agent.manifest.lifecycle.value}): "
                f"{agent.manifest.description}"
            )
        return "\n".join(lines)

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
            return str(self._config.discord.channel_id)
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
