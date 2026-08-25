"""Tests for alfred.domain.dispatch: the gated tool dispatcher."""

from __future__ import annotations

import pytest

from alfred.domain.dispatch import DispatchOutcome, ToolDispatcher
from alfred.domain.governance import PendingActions, Policy, WorkflowTrust
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import AgentManifest, Collections, ToolCall
from alfred.errors import AlfredError, ToolNotAllowedError, ToolNotFoundError
from alfred.ports.tools import CapabilityTier
from alfred.testing import FakeClock, FakeTools, MemoryStore

READ_TOOL = "current_time"
WRITE_TOOL = "log_note"
DESTRUCTIVE_TOOL = "delete_file"
MCP_WRITE_TOOL = "calendar.create_event"  # cross-system (an MCP server)
MCP_READ_TOOL = "calendar.list_events"  # cross-system but read-only
ALL_TOOLS = [READ_TOOL, WRITE_TOOL, DESTRUCTIVE_TOOL, MCP_WRITE_TOOL, MCP_READ_TOOL]


def make_agent(name: str = "trainer", allowed: list[str] | None = None) -> LoadedAgent:
    manifest = AgentManifest(
        name=name,
        description="test agent",
        allowed_tools=ALL_TOOLS if allowed is None else allowed,
    )
    return LoadedAgent(manifest=manifest, prompt="be useful")


def make_dispatcher(
    *,
    auto_approve_reversible: bool = True,
    dry_run_cross_system: bool = True,
    trust_after_approvals: int = 0,
) -> tuple[ToolDispatcher, FakeTools, MemoryStore, FakeClock, PendingActions]:
    tools = FakeTools()
    tools.add(READ_TOOL, tier=CapabilityTier.READ_ONLY)
    tools.add(WRITE_TOOL, tier=CapabilityTier.REVERSIBLE_WRITE)
    tools.add(DESTRUCTIVE_TOOL, tier=CapabilityTier.DESTRUCTIVE)
    tools.add(
        MCP_WRITE_TOOL, tier=CapabilityTier.REVERSIBLE_WRITE, source="mcp:calendar"
    )
    tools.add(MCP_READ_TOOL, tier=CapabilityTier.READ_ONLY, source="mcp:calendar")
    store = MemoryStore()
    clock = FakeClock()
    pending = PendingActions(store, clock)
    policy = Policy(
        auto_approve_reversible=auto_approve_reversible,
        dry_run_cross_system=dry_run_cross_system,
    )
    trust = WorkflowTrust(store, clock, threshold=trust_after_approvals)
    dispatcher = ToolDispatcher(tools, store, clock, policy, pending, trust=trust)
    return dispatcher, tools, store, clock, pending


async def audit_events(store: MemoryStore) -> list[str]:
    return [r["event"] for r in await store.query(Collections.AUDIT)]


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


async def test_allowlist_deny_raises_and_audits():
    dispatcher, tools, store, _, _ = make_dispatcher()
    agent = make_agent(allowed=[READ_TOOL])

    with pytest.raises(ToolNotAllowedError):
        await dispatcher.dispatch(agent, ToolCall(tool=DESTRUCTIVE_TOOL), "owner")

    assert tools.invocations == []
    records = await store.query(Collections.AUDIT)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "tool_denied"
    assert record["agent"] == "trainer"
    assert record["tool"] == DESTRUCTIVE_TOOL
    assert record["provenance"] == "owner"


async def test_empty_allowlist_denies_everything():
    dispatcher, tools, _, _, _ = make_dispatcher()
    agent = make_agent(allowed=[])
    with pytest.raises(ToolNotAllowedError):
        await dispatcher.dispatch(agent, ToolCall(tool=READ_TOOL), "owner")
    assert tools.invocations == []


async def test_unknown_tool_raises_not_found_and_audits():
    dispatcher, _, store, _, _ = make_dispatcher()
    agent = make_agent(allowed=["ghost_tool"])
    with pytest.raises(ToolNotFoundError):
        await dispatcher.dispatch(agent, ToolCall(tool="ghost_tool"), "owner")
    events = await audit_events(store)
    assert "tool_not_found" in events


# ---------------------------------------------------------------------------
# Tier gating
# ---------------------------------------------------------------------------


async def test_destructive_from_owner_is_gated_not_invoked():
    dispatcher, tools, store, _, _ = make_dispatcher()
    agent = make_agent()

    outcome = await dispatcher.dispatch(
        agent, ToolCall(tool=DESTRUCTIVE_TOOL, args={"path": "x"}), "owner"
    )

    assert isinstance(outcome, DispatchOutcome)
    assert outcome.result is None
    assert outcome.pending is not None
    assert outcome.pending.status == "pending"
    assert outcome.pending.tier == CapabilityTier.DESTRUCTIVE
    # Nothing executed.
    assert tools.invocations == []
    # The pending action is persisted.
    doc = await store.get(Collections.PENDING_ACTIONS, outcome.pending.id)
    assert doc is not None and doc["status"] == "pending"
    assert "tool_gated" in await audit_events(store)


async def test_reversible_from_external_is_gated():
    dispatcher, tools, store, _, _ = make_dispatcher()
    agent = make_agent()

    outcome = await dispatcher.dispatch(agent, ToolCall(tool=WRITE_TOOL), "external")

    assert outcome.result is None
    assert outcome.pending is not None
    assert tools.invocations == []
    assert "tool_gated" in await audit_events(store)


async def test_reversible_from_owner_with_auto_approve_executes_and_audits():
    dispatcher, tools, store, _, _ = make_dispatcher(auto_approve_reversible=True)
    agent = make_agent()

    outcome = await dispatcher.dispatch(
        agent, ToolCall(tool=WRITE_TOOL, args={"note": "hi"}), "owner"
    )

    assert outcome.pending is None
    assert outcome.result is not None and outcome.result.ok is True
    assert tools.invocations == [(WRITE_TOOL, {"note": "hi"})]
    records = await store.query(Collections.AUDIT)
    executed = [r for r in records if r["event"] == "tool_executed"]
    assert len(executed) == 1
    assert executed[0]["agent"] == "trainer"
    assert executed[0]["tool"] == WRITE_TOOL
    assert executed[0]["tier"] == "reversible_write"
    assert executed[0]["provenance"] == "owner"
    assert executed[0]["ok"] is True


async def test_reversible_from_owner_without_auto_approve_is_gated():
    dispatcher, tools, _, _, _ = make_dispatcher(auto_approve_reversible=False)
    agent = make_agent()
    outcome = await dispatcher.dispatch(agent, ToolCall(tool=WRITE_TOOL), "owner")
    assert outcome.pending is not None
    assert tools.invocations == []


async def test_read_only_from_external_executes():
    dispatcher, tools, _, _, _ = make_dispatcher()
    agent = make_agent()

    outcome = await dispatcher.dispatch(agent, ToolCall(tool=READ_TOOL), "external")

    assert outcome.pending is None
    assert outcome.result is not None and outcome.result.ok is True
    assert tools.invocations == [(READ_TOOL, {})]


# ---------------------------------------------------------------------------
# Dry run before cross-system action
# ---------------------------------------------------------------------------


async def test_cross_system_write_is_previewed_under_dry_run():
    # A reversible write to an MCP server would auto-approve on tier alone,
    # but the dry-run gate previews cross-system writes until trusted.
    dispatcher, tools, store, _, _ = make_dispatcher(
        auto_approve_reversible=True, dry_run_cross_system=True
    )
    agent = make_agent()

    outcome = await dispatcher.dispatch(agent, ToolCall(tool=MCP_WRITE_TOOL), "owner")

    assert outcome.result is None
    assert outcome.pending is not None
    assert "dry run" in outcome.pending.reason
    assert tools.invocations == []  # previewed, not executed
    gated = [r for r in await store.query(Collections.AUDIT) if r["event"] == "tool_gated"]
    assert len(gated) == 1


async def test_cross_system_write_executes_once_trusted():
    # With the dry run turned off, a cross-system reversible write auto-runs
    # like any other reversible write the owner has accepted.
    dispatcher, tools, _, _, _ = make_dispatcher(
        auto_approve_reversible=True, dry_run_cross_system=False
    )
    agent = make_agent()

    outcome = await dispatcher.dispatch(agent, ToolCall(tool=MCP_WRITE_TOOL), "owner")

    assert outcome.pending is None
    assert outcome.result is not None and outcome.result.ok is True
    assert tools.invocations == [(MCP_WRITE_TOOL, {})]


async def test_cross_system_read_is_not_previewed():
    # The dry run is for actions (writes), not reads: a cross-system read-only
    # call runs without a preview.
    dispatcher, tools, _, _, _ = make_dispatcher(dry_run_cross_system=True)
    agent = make_agent()

    outcome = await dispatcher.dispatch(agent, ToolCall(tool=MCP_READ_TOOL), "owner")

    assert outcome.pending is None
    assert outcome.result is not None and outcome.result.ok is True
    assert tools.invocations == [(MCP_READ_TOOL, {})]


async def test_local_write_is_unaffected_by_dry_run():
    # The dry run only previews external systems; local reversible writes
    # still auto-approve.
    dispatcher, tools, _, _, _ = make_dispatcher(
        auto_approve_reversible=True, dry_run_cross_system=True
    )
    agent = make_agent()

    outcome = await dispatcher.dispatch(agent, ToolCall(tool=WRITE_TOOL), "owner")

    assert outcome.pending is None
    assert tools.invocations == [(WRITE_TOOL, {})]


# ---------------------------------------------------------------------------
# execute_confirmed
# ---------------------------------------------------------------------------


async def test_execute_confirmed_happy_path_invokes_exactly_once():
    dispatcher, tools, store, _, _ = make_dispatcher()
    agent = make_agent()
    outcome = await dispatcher.dispatch(
        agent, ToolCall(tool=DESTRUCTIVE_TOOL, args={"path": "x"}), "owner"
    )
    assert outcome.pending is not None

    result = await dispatcher.execute_confirmed(outcome.pending.id, agent)

    assert result.ok is True
    assert tools.invocations == [(DESTRUCTIVE_TOOL, {"path": "x"})]
    doc = await store.get(Collections.PENDING_ACTIONS, outcome.pending.id)
    assert doc is not None and doc["status"] == "confirmed"
    executed = [
        r for r in await store.query(Collections.AUDIT) if r["event"] == "tool_executed"
    ]
    assert len(executed) == 1
    assert executed[0]["tool"] == DESTRUCTIVE_TOOL
    assert executed[0]["tier"] == "destructive"


async def test_execute_confirmed_with_no_agent_refuses():
    dispatcher, tools, store, _, _ = make_dispatcher()
    agent = make_agent()
    outcome = await dispatcher.dispatch(agent, ToolCall(tool=DESTRUCTIVE_TOOL), "owner")
    assert outcome.pending is not None

    with pytest.raises(ToolNotAllowedError):
        await dispatcher.execute_confirmed(outcome.pending.id, None)

    assert tools.invocations == []
    events = await audit_events(store)
    assert events.count("tool_denied") == 1


async def test_execute_confirmed_after_allowlist_revocation_refuses():
    dispatcher, tools, store, _, _ = make_dispatcher()
    agent = make_agent()
    outcome = await dispatcher.dispatch(agent, ToolCall(tool=DESTRUCTIVE_TOOL), "owner")
    assert outcome.pending is not None

    revoked = make_agent(allowed=[READ_TOOL, WRITE_TOOL])
    with pytest.raises(ToolNotAllowedError):
        await dispatcher.execute_confirmed(outcome.pending.id, revoked)

    assert tools.invocations == []
    assert "tool_denied" in await audit_events(store)


async def test_execute_confirmed_twice_raises_already_resolved():
    dispatcher, tools, _, _, _ = make_dispatcher()
    agent = make_agent()
    outcome = await dispatcher.dispatch(agent, ToolCall(tool=DESTRUCTIVE_TOOL), "owner")
    assert outcome.pending is not None

    await dispatcher.execute_confirmed(outcome.pending.id, agent)
    with pytest.raises(AlfredError):
        await dispatcher.execute_confirmed(outcome.pending.id, agent)
    # No double execution.
    assert len(tools.invocations) == 1


async def test_execute_confirmed_unknown_action_raises():
    dispatcher, _, _, _, _ = make_dispatcher()
    with pytest.raises(AlfredError):
        await dispatcher.execute_confirmed("missing", make_agent())


# ---------------------------------------------------------------------------
# Audit trail ordering
# ---------------------------------------------------------------------------


async def test_audit_trail_records_expected_events_in_order():
    dispatcher, _, store, _, _ = make_dispatcher()
    agent = make_agent()

    await dispatcher.dispatch(agent, ToolCall(tool=READ_TOOL), "owner")
    gated = await dispatcher.dispatch(agent, ToolCall(tool=DESTRUCTIVE_TOOL), "owner")
    assert gated.pending is not None
    with pytest.raises(ToolNotAllowedError):
        await dispatcher.dispatch(agent, ToolCall(tool="forbidden"), "external")
    await dispatcher.execute_confirmed(gated.pending.id, agent)

    assert await audit_events(store) == [
        "tool_executed",
        "tool_gated",
        "tool_denied",
        "pending_action_resolved",
        "tool_executed",
    ]


# ---------------------------------------------------------------------------
# The autonomy dial at dispatch time
# ---------------------------------------------------------------------------


async def test_trusted_workflow_skips_the_preview_and_the_audit_says_so():
    dispatcher, tools, store, clock, _ = make_dispatcher(trust_after_approvals=1)
    await WorkflowTrust(store, clock, 1).record_approval("trainer", MCP_WRITE_TOOL)

    outcome = await dispatcher.dispatch(
        make_agent(), ToolCall(tool=MCP_WRITE_TOOL), "owner"
    )

    assert outcome.pending is None
    assert outcome.result is not None and outcome.result.ok
    assert tools.invocations == [(MCP_WRITE_TOOL, {})]
    executed = [
        r
        for r in await store.query(Collections.AUDIT)
        if r["event"] == "tool_executed"
    ]
    assert executed[-1].get("trusted_workflow") is True


async def test_external_content_never_rides_earned_trust():
    dispatcher, tools, store, clock, _ = make_dispatcher(trust_after_approvals=1)
    await WorkflowTrust(store, clock, 1).record_approval("trainer", MCP_WRITE_TOOL)

    outcome = await dispatcher.dispatch(
        make_agent(), ToolCall(tool=MCP_WRITE_TOOL), "external"
    )

    assert outcome.pending is not None
    assert tools.invocations == []


async def test_destructive_cross_system_never_relaxes_even_when_trusted():
    dispatcher, tools, store, clock, _ = make_dispatcher(trust_after_approvals=1)
    tools.add(
        "calendar.delete_event",
        tier=CapabilityTier.DESTRUCTIVE,
        source="mcp:calendar",
    )
    agent = make_agent(allowed=[*ALL_TOOLS, "calendar.delete_event"])
    await WorkflowTrust(store, clock, 1).record_approval(
        "trainer", "calendar.delete_event"
    )

    outcome = await dispatcher.dispatch(
        agent, ToolCall(tool="calendar.delete_event"), "owner"
    )

    assert outcome.pending is not None
    assert tools.invocations == []


async def test_dial_off_means_no_trust_whatever_the_ledger_says():
    dispatcher, tools, store, clock, _ = make_dispatcher(trust_after_approvals=0)
    ledger = WorkflowTrust(store, clock, 5)
    for _ in range(10):
        await ledger.record_approval("trainer", MCP_WRITE_TOOL)

    outcome = await dispatcher.dispatch(
        make_agent(), ToolCall(tool=MCP_WRITE_TOOL), "owner"
    )

    assert outcome.pending is not None
    assert tools.invocations == []
