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
from alfred.domain.schemas import Lifecycle, PendingAction, Provenance, ToolCall
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
        self,
        agent: LoadedAgent,
        call: ToolCall,
        provenance: Provenance,
        bundle_id: str | None = None,
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

        # A non-local source is an external system (an MCP server); the
        # policy previews cross-system writes until the owner trusts them.
        cross_system = spec.source != "local"
        if self._policy.requires_confirmation(
            spec.tier, provenance, cross_system=cross_system
        ):
            # A model that ignores "awaits confirmation" feedback re-emits
            # the same gated call every round; without this reuse the owner
            # would face several confirm ids for one intent, and confirming
            # each would execute the destructive tool once per id.
            for existing in await self._pending.list_pending():
                if (
                    existing.agent == agent_name
                    and existing.call.tool == call.tool
                    and existing.call.args == call.args
                ):
                    await audit(
                        self._store,
                        self._clock,
                        "tool_gated",
                        agent=agent_name,
                        tool=call.tool,
                        tier=spec.tier.value,
                        provenance=provenance,
                        action_id=existing.id,
                        detail="reused existing pending action",
                    )
                    return DispatchOutcome(pending=existing)
            reason = call.reason
            if cross_system and self._policy.dry_run_cross_system:
                preview = "cross-system action: previewed before it runs (dry run)"
                reason = f"{reason}; {preview}" if reason else preview
            action = await self._pending.create(
                agent_name,
                call,
                spec.tier,
                provenance,
                reason=reason,
                bundle_id=bundle_id,
            )
            gated: dict[str, object] = {
                "agent": agent_name,
                "tool": call.tool,
                "tier": spec.tier.value,
                "provenance": provenance,
                "action_id": action.id,
            }
            if bundle_id is not None:
                gated["bundle_id"] = bundle_id
            await audit(self._store, self._clock, "tool_gated", **gated)
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

        if agent.manifest.lifecycle in (Lifecycle.PAUSED, Lifecycle.RETIRED):
            # Gated actions do not survive retirement or pausing: authority
            # comes from the CURRENT lifecycle, never the one at gating
            # time, or pausing a misbehaving agent would not stop its
            # already-queued writes.
            await audit(
                self._store,
                self._clock,
                "tool_denied",
                agent=agent.manifest.name,
                tool=action.call.tool,
                provenance=action.provenance,
                action_id=action_id,
                detail=f"agent is {agent.manifest.lifecycle.value}",
            )
            raise ToolNotAllowedError(
                f"agent '{agent.manifest.name}' is {agent.manifest.lifecycle.value}; "
                f"its gated actions no longer execute. Restore the agent's "
                f"lifecycle first if this action should still run."
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

        try:
            result = await self._tools.invoke(action.call.tool, action.call.args)
        except Exception:
            # The confirmation was consumed but nothing ran (tool vanished,
            # server down). Reopen so the owner can confirm again once the
            # fault is fixed; the deliberate refusals above stay consumed.
            await self._pending.reopen(action_id)
            await audit(
                self._store,
                self._clock,
                "execution_failed",
                agent=agent.manifest.name,
                tool=action.call.tool,
                tier=action.tier.value,
                provenance=action.provenance,
                action_id=action_id,
            )
            raise
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
