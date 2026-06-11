"""The gated tool dispatcher: every tool call passes through here.

Order of checks is load-bearing: allowlist first (deny by default), then
tool resolution, then tier policy. The allowlist comes before resolution
so an agent cannot even probe which tools exist outside its grant. Every
decision, deny, gate, or execute, leaves an audit record.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from alfred.domain.governance import PendingActions, Policy, audit
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import PendingAction, Provenance, ToolCall
from alfred.errors import ToolNotAllowedError, ToolNotFoundError
from alfred.ports.clock import ClockPort
from alfred.ports.store import StorePort
from alfred.ports.tools import ToolPort, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class DispatchOutcome(BaseModel):
    """What dispatch produced: a result when executed, a pending when gated."""

    result: ToolResult | None = None
    pending: PendingAction | None = None


class ToolDispatcher:
    """Routes agent tool calls through allowlist and tier governance."""

    def __init__(
        self,
        tools: ToolPort,
        store: StorePort,
        clock: ClockPort,
        policy: Policy,
        pending: PendingActions,
    ) -> None:
        self._tools = tools
        self._store = store
        self._clock = clock
        self._policy = policy
        self._pending = pending

    async def dispatch(
        self, agent: LoadedAgent, call: ToolCall, provenance: Provenance
    ) -> DispatchOutcome:
        agent_name = agent.manifest.name

        if call.tool not in agent.manifest.allowed_tools:
            await audit(
                self._store,
                self._clock,
                "tool_denied",
                agent=agent_name,
                tool=call.tool,
                provenance=provenance,
                detail="tool not in agent allowlist",
            )
            raise ToolNotAllowedError(
                f"agent '{agent_name}' is not allowed to invoke tool '{call.tool}'"
            )

        spec = await self._resolve_spec(call.tool)
        if spec is None:
            await audit(
                self._store,
                self._clock,
                "tool_not_found",
                agent=agent_name,
                tool=call.tool,
                provenance=provenance,
            )
            raise ToolNotFoundError(f"unknown tool: {call.tool}")

        if self._policy.requires_confirmation(spec.tier, provenance):
            action = await self._pending.create(
                agent_name, call, spec.tier, provenance, reason=call.reason
            )
            await audit(
                self._store,
                self._clock,
                "tool_gated",
                agent=agent_name,
                tool=call.tool,
                tier=spec.tier.value,
                provenance=provenance,
                action_id=action.id,
            )
            return DispatchOutcome(pending=action)

        result = await self._tools.invoke(call.tool, call.args)
        await audit(
            self._store,
            self._clock,
            "tool_executed",
            agent=agent_name,
            tool=call.tool,
            tier=spec.tier.value,
            provenance=provenance,
            ok=result.ok,
        )
        return DispatchOutcome(result=result)

    async def execute_confirmed(
        self, action_id: str, agent: LoadedAgent | None
    ) -> ToolResult:
        action = await self._pending.resolve(action_id, approved=True)

        if agent is None:
            # The agent behind the gated call no longer exists, so there
            # is no authority left to execute under.
            await audit(
                self._store,
                self._clock,
                "tool_denied",
                agent=action.agent,
                tool=action.call.tool,
                provenance=action.provenance,
                action_id=action_id,
                detail="agent no longer exists",
            )
            raise ToolNotAllowedError(
                f"agent '{action.agent}' no longer exists; "
                f"confirmed action {action_id} refused"
            )

        if action.call.tool not in agent.manifest.allowed_tools:
            # Allowlist revoked between gating and confirmation: the
            # CURRENT manifest wins, never the snapshot from gating time.
            await audit(
                self._store,
                self._clock,
                "tool_denied",
                agent=agent.manifest.name,
                tool=action.call.tool,
                provenance=action.provenance,
                action_id=action_id,
                detail="tool removed from allowlist since gating",
            )
            raise ToolNotAllowedError(
                f"agent '{agent.manifest.name}' is no longer allowed to invoke "
                f"tool '{action.call.tool}'"
            )

        result = await self._tools.invoke(action.call.tool, action.call.args)
        await audit(
            self._store,
            self._clock,
            "tool_executed",
            agent=agent.manifest.name,
            tool=action.call.tool,
            tier=action.tier.value,
            provenance=action.provenance,
            ok=result.ok,
            action_id=action_id,
        )
        return result

    async def _resolve_spec(self, name: str) -> ToolSpec | None:
        for spec in await self._tools.list_tools():
            if spec.name == name:
                return spec
        return None
