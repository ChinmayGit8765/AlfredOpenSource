"""Tests for alfred.domain.dispatch: the gated tool dispatcher."""

from __future__ import annotations

import pytest

from alfred.domain.dispatch import DispatchOutcome, ToolDispatcher
from alfred.domain.governance import PendingActions, Policy
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import AgentManifest, Collections, ToolCall
from alfred.errors import AlfredError, ToolNotAllowedError, ToolNotFoundError
from alfred.ports.tools import CapabilityTier
from alfred.testing import FakeClock, FakeTools, MemoryStore

READ_TOOL = "current_time"
WRITE_TOOL = "log_note"
DESTRUCTIVE_TOOL = "delete_file"
ALL_TOOLS = [READ_TOOL, WRITE_TOOL, DESTRUCTIVE_TOOL]


def make_agent(name: str = "trainer", allowed: list[str] | None = None) -> LoadedAgent:
    manifest = AgentManifest(
        name=name,
        description="test agent",
        allowed_tools=ALL_TOOLS if allowed is None else allowed,
    )
    return LoadedAgent(manifest=manifest, prompt="be useful")


def make_dispatcher(
    *, auto_approve_reversible: bool = True
) -> tuple[ToolDispatcher, FakeTools, MemoryStore, FakeClock, PendingActions]:
    tools = FakeTools()
    tools.add(READ_TOOL, tier=CapabilityTier.READ_ONLY)
    tools.add(WRITE_TOOL, tier=CapabilityTier.REVERSIBLE_WRITE)
    tools.add(DESTRUCTIVE_TOOL, tier=CapabilityTier.DESTRUCTIVE)
    store = MemoryStore()
    clock = FakeClock()
    pending = PendingActions(store, clock)
    policy = Policy(auto_approve_reversible=auto_approve_reversible)
    dispatcher = ToolDispatcher(tools, store, clock, policy, pending)
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
