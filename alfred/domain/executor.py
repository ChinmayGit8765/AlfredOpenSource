"""Running one agent: the orchestration heart.

One run is one governed conversation: the agent's prompt is wrapped with
the governance preamble, the owner model, adherence pressure, and the
exact tools it may call; every tool call passes through the dispatcher;
gated calls surface as pending actions instead of executing. The executor
never widens access and never lets a tool failure crash the run.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.feedback import plan_adjustment_hint
from alfred.domain.governance import audit
from alfred.domain.memory import MemoryService
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import (
    AgentReply,
    Collections,
    ExecutionResult,
    Plan,
    Provenance,
    ToolCall,
    new_id,
)
from alfred.domain.structured import structured_call
from alfred.domain.user_model import UserModelService
from alfred.errors import ToolNotAllowedError, ToolNotFoundError
from alfred.ports import ClockPort, ModelMessage, ModelPort, StorePort, ToolPort
from alfred.ports.tools import ToolSpec

logger = logging.getLogger(__name__)

_GOVERNANCE_PREAMBLE = (
    "Governance rules, binding: you may only call the tools listed below; "
    "any other call is refused. Tools carry capability tiers (read_only, "
    "reversible_write, destructive); destructive and otherwise gated calls "
    "are never executed immediately, they await explicit owner confirmation, "
    "so when a call is gated word your reply to the owner accordingly. Never "
    "fabricate a tool result: if no tool message carries the result, you do "
    "not have it. Observations are short factual notes about the owner worth "
    "remembering."
)

_OUTPUT_CONTRACT = (
    "Output contract: respond with a single AgentReply JSON object. Fields: "
    "reply is your message to the owner; plan is included only when you are "
    "delivering a plan, with items carrying day, time, duration_min, load, "
    "and anchor; tool_calls is included when you need tool results before "
    "you can finish; observations are short factual notes worth persisting; "
    "done is false only when you need tool results before finishing, "
    "otherwise true."
)

_CONTINUE_PROMPT = (
    "Tool results and notices were provided above as tool messages. Use "
    "them to finish your reply to the owner. Set done to true unless you "
    "still need more tool results."
)


def _render_tool_specs(specs: list[ToolSpec]) -> str:
    if not specs:
        return "No tools are available to you; do not request any."
    lines = ["Tools available to you:"]
    for spec in specs:
        lines.append(
            f"- {spec.name} (tier: {spec.tier.value}): {spec.description}; "
            f"parameters: {json.dumps(spec.parameters)}"
        )
    return "\n".join(lines)


class AgentExecutor:
    """Runs one agent end to end under full governance."""

    def __init__(
        self,
        model: ModelPort,
        tools: ToolPort,
        dispatcher: ToolDispatcher,
        user_model: UserModelService,
        store: StorePort,
        clock: ClockPort,
        memory: MemoryService | None = None,
    ) -> None:
        self._model = model
        self._tools = tools
        self._dispatcher = dispatcher
        self._user_model = user_model
        self._store = store
        self._clock = clock
        self._memory = memory

    async def run(
        self,
        agent: LoadedAgent,
        *,
        text: str,
        provenance: Provenance,
        max_rounds: int = 3,
    ) -> ExecutionResult:
        name = agent.manifest.name
        system = await self._assemble_system_prompt(agent, inbound_text=text)
        result = ExecutionResult(agent=name)
        # One run is one intent: every call gated during it shares this
        # bundle, so several writes across systems surface to the owner as
        # one composed preview with one confirm, not a pile of ids.
        bundle_id = new_id()

        conversation: list[ModelMessage] = []
        current_user = text
        rounds_used = 0
        tool_call_count = 0
        plan_dropped = False
        done = False

        for _ in range(max_rounds):
            rounds_used += 1
            reply = await structured_call(
                self._model,
                schema=AgentReply,
                system=system,
                user=current_user,
                history=list(conversation) or None,
                options=agent.manifest.model,
            )

            if reply.reply.strip():
                result.replies.append(reply.reply)

            if reply.observations and provenance == "external":
                # Untrusted content must never write into the shared owner
                # model: a persisted observation re-enters every agent's
                # system prompt with owner authority and feeds reflection,
                # which makes it a durable prompt-injection channel.
                # Dropped and audited, never stored.
                await audit(
                    self._store,
                    self._clock,
                    "observations_dropped",
                    agent=name,
                    provenance=provenance,
                    count=len(reply.observations),
                )
            else:
                for note in reply.observations:
                    await self._user_model.record_observation(
                        source=name, kind="insight", text=note
                    )
                    result.observations.append(note)

            if reply.plan is not None:
                if agent.manifest.emits_plans:
                    result.plan = self._stamp_plan(reply.plan, name)
                else:
                    # Scheduled runs hand every agent a planning prompt, so
                    # a meta agent will sometimes obey the text over its own
                    # rules; the flag, not the prompt, is what keeps its
                    # plan out of the store, the Conductor, and the peer
                    # digests.
                    plan_dropped = True
                    logger.warning(
                        "agent %s emitted a plan but emits_plans is false; dropped",
                        name,
                    )

            conversation.append(ModelMessage(role="user", content=current_user))
            conversation.append(
                ModelMessage(role="assistant", content=reply.model_dump_json())
            )

            fed_back = 0
            for call in reply.tool_calls:
                tool_call_count += 1
                feedback = await self._handle_tool_call(
                    agent, call, provenance, result, bundle_id
                )
                conversation.append(ModelMessage(role="tool", content=feedback))
                fed_back += 1

            done = reply.done
            if done or fed_back == 0:
                # Without new tool messages another round would just replay
                # the same context, so stop even when the model says not done.
                break
            current_user = _CONTINUE_PROMPT

        if not done:
            result.replies.append(
                f"Note: this run stopped at its round limit ({max_rounds} "
                "rounds) before the agent reported completion."
            )

        # Persist the plan once per run, after the rounds settle. Persisting
        # inside the loop appended a duplicate document whenever the model
        # re-emitted its plan in a later round (common after tool results).
        if result.plan is not None:
            await self._store.append(
                Collections.PLANS, result.plan.model_dump(mode="json")
            )

        audit_data: dict[str, object] = {
            "agent": name,
            "provenance": provenance,
            "rounds": rounds_used,
            "tool_calls": tool_call_count,
        }
        if result.plan is not None:
            audit_data["plan_id"] = result.plan.id
        if plan_dropped:
            audit_data["plan_dropped"] = True
        await audit(self._store, self._clock, "agent_run", **audit_data)

        logger.info(
            "agent run complete: agent=%s rounds=%d tool_calls=%d pending=%d",
            name,
            rounds_used,
            tool_call_count,
            len(result.pending),
        )
        return result

    async def _assemble_system_prompt(
        self, agent: LoadedAgent, *, inbound_text: str = ""
    ) -> str:
        sections = [agent.prompt, _GOVERNANCE_PREAMBLE]
        sections.append(await self._user_model.summary_for_prompt())

        # Cohesion: every agent is briefed with the memories the message
        # touches and with what its peers have already planned this week,
        # so the system behaves like one assistant, not a row of silos.
        if self._memory is not None and inbound_text:
            memory_block = await self._memory.context_for(inbound_text)
            if memory_block:
                sections.append(memory_block)
        peers = await self._peer_plan_digest(agent.manifest.name)
        if peers:
            sections.append(peers)

        profile = await self._user_model.get_profile()
        stats = profile.adherence.get(agent.manifest.name)
        hint = plan_adjustment_hint(stats) if stats is not None else ""
        if hint:
            sections.append(f"Adherence hint for this agent: {hint}")

        allowed = set(agent.manifest.allowed_tools)
        specs = [s for s in await self._tools.list_tools() if s.name in allowed]
        sections.append(_render_tool_specs(specs))
        sections.append(_OUTPUT_CONTRACT)
        return "\n\n".join(section for section in sections if section.strip())

    async def _peer_plan_digest(self, agent_name: str) -> str:
        """One line per other agent's current-week plan; "" when none exist."""
        today = self._clock.now().date()
        week_of = today - timedelta(days=today.weekday())
        docs = await self._store.query(
            Collections.PLANS, limit=100, newest_first=True
        )
        lines: list[str] = []
        seen: set[str] = set()
        for doc in docs:
            data = {k: v for k, v in doc.items() if k != "_key"}
            try:
                plan = Plan.model_validate(data)
            except ValueError:
                continue
            if (
                not plan.agent
                or plan.agent == agent_name
                or plan.agent in seen
                or plan.week_of != week_of
            ):
                continue
            seen.add(plan.agent)
            titles = "; ".join(item.title for item in plan.items[:4])
            lines.append(
                f"- {plan.agent}: {len(plan.items)} item(s), load "
                f"{plan.total_load}: {titles}"
            )
        if not lines:
            return ""
        return (
            "Other agents' plans for this week (respect their load; the "
            "owner's capacity is shared):\n" + "\n".join(lines)
        )

    async def _handle_tool_call(
        self,
        agent: LoadedAgent,
        call: ToolCall,
        provenance: Provenance,
        result: ExecutionResult,
        bundle_id: str,
    ) -> str:
        """Dispatch one tool call; return the tool message to feed back."""
        try:
            outcome = await self._dispatcher.dispatch(
                agent, call, provenance, bundle_id=bundle_id
            )
        except (ToolNotAllowedError, ToolNotFoundError) as exc:
            # The dispatcher already audited the violation; the run must
            # survive and the model must hear an honest refusal.
            return f"Tool call '{call.tool}' was refused: {exc}"

        if outcome.pending is not None:
            result.pending.append(outcome.pending)
            return (
                f"Tool '{call.tool}' was not executed: it awaits owner "
                f"confirmation (pending action id {outcome.pending.id}). "
                "Word your reply to the owner accordingly."
            )

        if outcome.result is not None:
            return f"Tool '{call.tool}' result: {outcome.result.model_dump_json()}"
        return f"Tool '{call.tool}' produced no result."

    def _stamp_plan(self, plan: Plan, agent_name: str) -> Plan:
        now = self._clock.now()
        week_of = plan.week_of
        if week_of is None:
            today = now.date()
            week_of = today - timedelta(days=today.weekday())
        return plan.model_copy(
            update={"agent": agent_name, "created_at": now, "week_of": week_of}
        )
