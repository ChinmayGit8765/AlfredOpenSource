"""Tests for alfred.domain.governance: policy, pending actions, proposals, audit."""

from __future__ import annotations

import pytest

from alfred.domain.governance import PendingActions, Policy, Proposals, audit
from alfred.domain.schemas import Collections, Proposal, ProposalKind, ToolCall
from alfred.errors import AlfredError
from alfred.ports.tools import CapabilityTier
from alfred.testing import FakeClock, MemoryStore

# ---------------------------------------------------------------------------
# Policy truth table
# ---------------------------------------------------------------------------

READ_ONLY = CapabilityTier.READ_ONLY
REVERSIBLE = CapabilityTier.REVERSIBLE_WRITE
DESTRUCTIVE = CapabilityTier.DESTRUCTIVE

# (tier, provenance, auto_approve_reversible, expected requires_confirmation)
TRUTH_TABLE = [
    (READ_ONLY, "owner", True, False),
    (READ_ONLY, "owner", False, False),
    (READ_ONLY, "scheduler", True, False),
    (READ_ONLY, "scheduler", False, False),
    (READ_ONLY, "external", True, False),
    (READ_ONLY, "external", False, False),
    (REVERSIBLE, "owner", True, False),
    (REVERSIBLE, "owner", False, True),
    (REVERSIBLE, "scheduler", True, False),
    (REVERSIBLE, "scheduler", False, True),
    (REVERSIBLE, "external", True, True),
    (REVERSIBLE, "external", False, True),
    (DESTRUCTIVE, "owner", True, True),
    (DESTRUCTIVE, "owner", False, True),
    (DESTRUCTIVE, "scheduler", True, True),
    (DESTRUCTIVE, "scheduler", False, True),
    (DESTRUCTIVE, "external", True, True),
    (DESTRUCTIVE, "external", False, True),
]


@pytest.mark.parametrize("tier,provenance,auto_approve,expected", TRUTH_TABLE)
def test_policy_truth_table(tier, provenance, auto_approve, expected):
    policy = Policy(auto_approve_reversible=auto_approve)
    assert policy.requires_confirmation(tier, provenance) is expected


def test_policy_defaults_to_auto_approving_reversible():
    assert Policy().requires_confirmation(REVERSIBLE, "owner") is False


# ---------------------------------------------------------------------------
# audit()
# ---------------------------------------------------------------------------


async def test_audit_appends_event_with_timestamp_and_data():
    store = MemoryStore()
    clock = FakeClock()
    await audit(store, clock, "tool_executed", agent="trainer", ok=True)

    records = await store.query(Collections.AUDIT)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "tool_executed"
    assert record["at"] == clock.now().isoformat()
    assert record["agent"] == "trainer"
    assert record["ok"] is True


async def test_audit_records_preserve_order():
    store = MemoryStore()
    clock = FakeClock()
    await audit(store, clock, "first")
    await audit(store, clock, "second")
    await audit(store, clock, "third")

    events = [r["event"] for r in await store.query(Collections.AUDIT)]
    assert events == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# PendingActions
# ---------------------------------------------------------------------------


def make_pending(ttl_hours: int = 24) -> tuple[PendingActions, MemoryStore, FakeClock]:
    store = MemoryStore()
    clock = FakeClock()
    return PendingActions(store, clock, ttl_hours=ttl_hours), store, clock


async def test_create_persists_and_stamps_created_at():
    pending, store, clock = make_pending()
    call = ToolCall(tool="delete_file", args={"path": "x"})
    action = await pending.create("trainer", call, DESTRUCTIVE, "owner", reason="cleanup")

    assert action.status == "pending"
    assert action.created_at == clock.now()
    doc = await store.get(Collections.PENDING_ACTIONS, action.id)
    assert doc is not None
    assert doc["status"] == "pending"
    assert doc["agent"] == "trainer"
    assert doc["call"]["tool"] == "delete_file"


async def test_get_returns_none_for_unknown_id():
    pending, _, _ = make_pending()
    assert await pending.get("nope") is None


async def test_list_pending_returns_fresh_actions():
    pending, _, _ = make_pending()
    call = ToolCall(tool="t")
    a = await pending.create("a1", call, DESTRUCTIVE, "owner")
    b = await pending.create("a2", call, REVERSIBLE, "external")

    listed = await pending.list_pending()
    assert {p.id for p in listed} == {a.id, b.id}


async def test_list_pending_expires_stale_actions_via_clock_advance():
    pending, store, clock = make_pending(ttl_hours=24)
    call = ToolCall(tool="t")
    old = await pending.create("a1", call, DESTRUCTIVE, "owner")
    clock.advance(hours=25)
    fresh = await pending.create("a2", call, DESTRUCTIVE, "owner")

    listed = await pending.list_pending()
    assert [p.id for p in listed] == [fresh.id]
    # The stale action must be persisted back as expired, not deleted.
    doc = await store.get(Collections.PENDING_ACTIONS, old.id)
    assert doc is not None
    assert doc["status"] == "expired"


async def test_resolve_approved_marks_confirmed():
    pending, store, _ = make_pending()
    action = await pending.create("a1", ToolCall(tool="t"), DESTRUCTIVE, "owner")

    resolved = await pending.resolve(action.id, approved=True)
    assert resolved.status == "confirmed"
    doc = await store.get(Collections.PENDING_ACTIONS, action.id)
    assert doc is not None and doc["status"] == "confirmed"


async def test_resolve_rejected_marks_rejected():
    pending, _, _ = make_pending()
    action = await pending.create("a1", ToolCall(tool="t"), DESTRUCTIVE, "owner")

    resolved = await pending.resolve(action.id, approved=False)
    assert resolved.status == "rejected"


async def test_resolve_unknown_id_raises():
    pending, _, _ = make_pending()
    with pytest.raises(AlfredError):
        await pending.resolve("missing", approved=True)


async def test_resolve_already_resolved_raises():
    pending, _, _ = make_pending()
    action = await pending.create("a1", ToolCall(tool="t"), DESTRUCTIVE, "owner")
    await pending.resolve(action.id, approved=True)
    with pytest.raises(AlfredError):
        await pending.resolve(action.id, approved=True)


async def test_resolve_stale_action_expires_and_raises():
    pending, store, clock = make_pending(ttl_hours=24)
    action = await pending.create("a1", ToolCall(tool="t"), DESTRUCTIVE, "owner")
    clock.advance(hours=25)
    with pytest.raises(AlfredError):
        await pending.resolve(action.id, approved=True)
    doc = await store.get(Collections.PENDING_ACTIONS, action.id)
    assert doc is not None and doc["status"] == "expired"


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


def make_proposal(**overrides) -> Proposal:
    defaults = dict(
        kind=ProposalKind.PROMPT_CHANGE,
        agent="trainer",
        summary="tweak tone",
    )
    defaults.update(overrides)
    return Proposal(**defaults)


async def test_proposal_create_stamps_and_forces_pending():
    store = MemoryStore()
    clock = FakeClock()
    proposals = Proposals(store, clock)
    # Even a proposal arriving pre-marked approved must be stored pending.
    created = await proposals.create(make_proposal(status="approved", touches_safety=True))

    assert created.status == "pending"
    assert created.created_at == clock.now()
    doc = await store.get(Collections.PROPOSALS, created.id)
    assert doc is not None and doc["status"] == "pending"


async def test_proposal_list_pending_excludes_resolved():
    store = MemoryStore()
    proposals = Proposals(store, FakeClock())
    first = await proposals.create(make_proposal())
    second = await proposals.create(make_proposal(summary="other"))
    await proposals.resolve(first.id, approved=True)

    listed = await proposals.list_pending()
    assert [p.id for p in listed] == [second.id]


async def test_proposal_resolve_approves_and_rejects():
    store = MemoryStore()
    proposals = Proposals(store, FakeClock())
    a = await proposals.create(make_proposal())
    b = await proposals.create(make_proposal(summary="other"))

    approved = await proposals.resolve(a.id, approved=True)
    rejected = await proposals.resolve(b.id, approved=False)
    assert approved.status == "approved"
    assert rejected.status == "rejected"
    doc = await store.get(Collections.PROPOSALS, a.id)
    assert doc is not None and doc["status"] == "approved"


async def test_proposal_resolve_unknown_or_resolved_raises():
    proposals = Proposals(MemoryStore(), FakeClock())
    with pytest.raises(AlfredError):
        await proposals.resolve("missing", approved=True)
    created = await proposals.create(make_proposal())
    await proposals.resolve(created.id, approved=False)
    with pytest.raises(AlfredError):
        await proposals.resolve(created.id, approved=True)
