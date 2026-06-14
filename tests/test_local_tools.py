"""Tests for alfred.adapters.local_tools: ALFRED's built-in tools."""

from __future__ import annotations

from datetime import date

import pytest

from alfred.adapters.local_tools import LocalToolAdapter
from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.governance import PendingActions, Policy
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import (
    AdherenceStats,
    AgentManifest,
    Collections,
    Observation,
    Outcome,
    OutcomeStatus,
    Plan,
    PlanItem,
    ToolCall,
    UserProfile,
)
from alfred.errors import ToolNotFoundError
from alfred.ports.tools import CapabilityTier
from alfred.testing import FakeClock, MemoryStore

EXPECTED_ORDER = [
    "current_time",
    "list_plans",
    "list_recent_outcomes",
    "list_agents_state",
    "log_note",
    "recall_memories",
    "remember_fact",
]


def make_adapter() -> tuple[LocalToolAdapter, MemoryStore, FakeClock]:
    store = MemoryStore()
    clock = FakeClock()
    return LocalToolAdapter(store, clock), store, clock


async def seed_plan(
    store: MemoryStore,
    *,
    agent: str,
    titles_days: list[tuple[str, str | None]],
    week_of: str | None = None,
    load: int = 1,
) -> Plan:
    plan = Plan(
        agent=agent,
        week_of=date.fromisoformat(week_of) if week_of else None,
        items=[
            PlanItem(title=title, day=day, load=load) for title, day in titles_days
        ],
    )
    await store.append(Collections.PLANS, plan.model_dump(mode="json"))
    return plan


async def seed_outcome(store: MemoryStore, *, agent: str, status: OutcomeStatus) -> Outcome:
    outcome = Outcome(agent=agent, status=status)
    await store.append(Collections.OUTCOMES, outcome.model_dump(mode="json"))
    return outcome


# ---------------------------------------------------------------------------
# Specs: order, tiers, parameter schemas
# ---------------------------------------------------------------------------


async def test_list_tools_stable_order():
    adapter, _, _ = make_adapter()
    first = [spec.name for spec in await adapter.list_tools()]
    second = [spec.name for spec in await adapter.list_tools()]
    assert first == EXPECTED_ORDER
    assert second == EXPECTED_ORDER


async def test_tiers_are_exactly_as_specified():
    # The dispatcher gates on these values; assert them explicitly.
    adapter, _, _ = make_adapter()
    tiers = {spec.name: spec.tier for spec in await adapter.list_tools()}
    assert tiers["current_time"] == CapabilityTier.READ_ONLY
    assert tiers["list_plans"] == CapabilityTier.READ_ONLY
    assert tiers["list_recent_outcomes"] == CapabilityTier.READ_ONLY
    assert tiers["list_agents_state"] == CapabilityTier.READ_ONLY
    assert tiers["log_note"] == CapabilityTier.REVERSIBLE_WRITE
    assert tiers["log_note"].value == "reversible_write"


async def test_parameter_schemas_are_honest():
    adapter, _, _ = make_adapter()
    params = {spec.name: spec.parameters for spec in await adapter.list_tools()}

    for name in ("current_time", "list_agents_state"):
        assert params[name]["type"] == "object"
        assert params[name]["properties"] == {}

    plans = params["list_plans"]
    assert plans["properties"]["agent"]["type"] == "string"
    assert plans["properties"]["limit"]["maximum"] == 20

    outcomes = params["list_recent_outcomes"]
    assert outcomes["properties"]["agent"]["type"] == "string"
    assert outcomes["properties"]["limit"]["maximum"] == 50

    note = params["log_note"]
    assert note["properties"]["text"]["type"] == "string"
    assert note["required"] == ["text"]


async def test_specs_declare_local_source():
    adapter, _, _ = make_adapter()
    assert all(spec.source == "local" for spec in await adapter.list_tools())


# ---------------------------------------------------------------------------
# current_time
# ---------------------------------------------------------------------------


async def test_current_time_returns_iso_and_weekday():
    adapter, _, clock = make_adapter()
    result = await adapter.invoke("current_time", {})
    assert result.ok is True
    assert result.content["iso"] == clock.now().isoformat()
    # FakeClock defaults to Monday 2026-01-05.
    assert result.content["weekday"] == "Monday"


# ---------------------------------------------------------------------------
# list_plans
# ---------------------------------------------------------------------------


async def test_list_plans_newest_first_with_compact_fields():
    adapter, store, _ = make_adapter()
    older = await seed_plan(
        store,
        agent="trainer",
        titles_days=[("run", "mon"), ("lift", "wed")],
        week_of="2026-01-05",
        load=2,
    )
    newer = await seed_plan(store, agent="study", titles_days=[("read", None)])

    result = await adapter.invoke("list_plans", {})
    assert result.ok is True
    assert [p["id"] for p in result.content] == [newer.id, older.id]

    summary = result.content[1]
    assert summary["agent"] == "trainer"
    assert summary["week_of"] == "2026-01-05"
    assert summary["item_count"] == 2
    assert summary["total_load"] == 4
    assert summary["titles_by_day"] == {"mon": ["run"], "wed": ["lift"]}
    assert result.content[0]["titles_by_day"] == {"unscheduled": ["read"]}


async def test_list_plans_filters_by_agent_and_limits():
    adapter, store, _ = make_adapter()
    for i in range(3):
        await seed_plan(store, agent="trainer", titles_days=[(f"t{i}", "mon")])
    await seed_plan(store, agent="study", titles_days=[("read", "tue")])

    filtered = await adapter.invoke("list_plans", {"agent": "trainer"})
    assert filtered.ok is True
    assert len(filtered.content) == 3
    assert all(p["agent"] == "trainer" for p in filtered.content)

    limited = await adapter.invoke("list_plans", {"limit": 2})
    assert limited.ok is True
    assert len(limited.content) == 2


async def test_list_plans_rejects_limit_above_cap():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("list_plans", {"limit": 21})
    assert result.ok is False
    assert result.error is not None and "list_plans" in result.error


# ---------------------------------------------------------------------------
# list_recent_outcomes
# ---------------------------------------------------------------------------


async def test_list_recent_outcomes_newest_first_and_filtered():
    adapter, store, _ = make_adapter()
    first = await seed_outcome(store, agent="trainer", status=OutcomeStatus.DONE)
    second = await seed_outcome(store, agent="trainer", status=OutcomeStatus.MISSED)
    await seed_outcome(store, agent="study", status=OutcomeStatus.PARTIAL)

    result = await adapter.invoke("list_recent_outcomes", {"agent": "trainer"})
    assert result.ok is True
    assert [o["id"] for o in result.content] == [second.id, first.id]
    assert result.content[0]["status"] == "missed"


async def test_list_recent_outcomes_respects_limit():
    adapter, store, _ = make_adapter()
    for _ in range(4):
        await seed_outcome(store, agent="trainer", status=OutcomeStatus.DONE)
    result = await adapter.invoke("list_recent_outcomes", {"limit": 2})
    assert result.ok is True
    assert len(result.content) == 2


async def test_list_recent_outcomes_rejects_limit_above_cap():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("list_recent_outcomes", {"limit": 51})
    assert result.ok is False


# ---------------------------------------------------------------------------
# list_agents_state
# ---------------------------------------------------------------------------


async def test_list_agents_state_empty_without_profile():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("list_agents_state", {})
    assert result.ok is True
    assert result.content == {}


async def test_list_agents_state_returns_adherence_dicts():
    adapter, store, _ = make_adapter()
    profile = UserProfile(
        adherence={
            "trainer": AdherenceStats(done=3, missed=1),
            "study": AdherenceStats(done=1, consecutive_misses=2, missed=2),
        }
    )
    await store.put(Collections.PROFILE, "current", profile.model_dump(mode="json"))

    result = await adapter.invoke("list_agents_state", {})
    assert result.ok is True
    trainer = result.content["trainer"]
    assert trainer["done"] == 3
    assert trainer["missed"] == 1
    assert trainer["total"] == 4
    assert trainer["rate"] == 0.75
    assert result.content["study"]["consecutive_misses"] == 2


# ---------------------------------------------------------------------------
# log_note
# ---------------------------------------------------------------------------


async def test_log_note_appends_observation_and_returns_key():
    adapter, store, clock = make_adapter()
    result = await adapter.invoke("log_note", {"text": "slept badly before the run"})
    assert result.ok is True

    docs = await store.query(Collections.OBSERVATIONS)
    assert len(docs) == 1
    assert result.content["key"] == docs[0]["_key"]

    observation = Observation.model_validate(
        {k: v for k, v in docs[0].items() if k != "_key"}
    )
    assert observation.source == "tool"
    assert observation.kind == "event"
    assert observation.text == "slept badly before the run"
    assert observation.at == clock.now()


async def test_log_note_missing_text_fails_without_writing():
    adapter, store, _ = make_adapter()
    result = await adapter.invoke("log_note", {})
    assert result.ok is False
    assert result.error is not None
    assert await store.query(Collections.OBSERVATIONS) == []


async def test_log_note_empty_text_fails():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("log_note", {"text": ""})
    assert result.ok is False


# ---------------------------------------------------------------------------
# memory tools: remember_fact / recall_memories
# ---------------------------------------------------------------------------


async def test_remember_then_recall_through_the_tool_interface():
    # The agent-facing contract (arg mapping and result shaping) for the two
    # memory tools, exercised through invoke rather than only listed.
    adapter, _, _ = make_adapter()

    filed = await adapter.invoke(
        "remember_fact",
        {"text": "physio said no overhead pressing until March", "kind": "fact"},
    )
    assert filed.ok is True
    assert filed.content["filed"] == "physio said no overhead pressing until March"
    memory_id = filed.content["id"]

    found = await adapter.invoke("recall_memories", {"query": "overhead pressing"})
    assert found.ok is True
    assert isinstance(found.content, list) and len(found.content) == 1
    hit = found.content[0]
    assert hit["id"] == memory_id
    assert set(hit) == {"id", "kind", "text", "when"}
    assert hit["kind"] == "fact"
    assert "overhead pressing" in hit["text"]


# ---------------------------------------------------------------------------
# invoke error handling
# ---------------------------------------------------------------------------


async def test_unknown_tool_raises_not_found():
    adapter, _, _ = make_adapter()
    with pytest.raises(ToolNotFoundError):
        await adapter.invoke("ghost_tool", {})


async def test_unexpected_args_fail_as_result_not_exception():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("current_time", {"surprise": 1})
    assert result.ok is False
    assert result.error is not None


async def test_handler_fault_becomes_failed_result():
    adapter, store, _ = make_adapter()
    # A malformed stored document makes the handler fault internally.
    await store.append(Collections.PLANS, {"items": "not-a-list"})
    result = await adapter.invoke("list_plans", {})
    assert result.ok is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# Integration through the real ToolDispatcher
# ---------------------------------------------------------------------------


def make_agent(allowed: list[str]) -> LoadedAgent:
    manifest = AgentManifest(
        name="trainer", description="test agent", allowed_tools=allowed
    )
    return LoadedAgent(manifest=manifest, prompt="be useful")


def make_dispatcher(
    adapter: LocalToolAdapter,
    store: MemoryStore,
    clock: FakeClock,
    *,
    auto_approve_reversible: bool = True,
) -> ToolDispatcher:
    policy = Policy(auto_approve_reversible=auto_approve_reversible)
    pending = PendingActions(store, clock)
    return ToolDispatcher(adapter, store, clock, policy, pending)


async def test_dispatcher_auto_executes_read_only():
    adapter, store, clock = make_adapter()
    dispatcher = make_dispatcher(adapter, store, clock)
    agent = make_agent(["current_time", "log_note"])

    outcome = await dispatcher.dispatch(agent, ToolCall(tool="current_time"), "owner")

    assert outcome.pending is None
    assert outcome.result is not None and outcome.result.ok is True
    assert outcome.result.content["weekday"] == "Monday"


async def test_dispatcher_gates_log_note_under_strict_policy():
    adapter, store, clock = make_adapter()
    dispatcher = make_dispatcher(adapter, store, clock, auto_approve_reversible=False)
    agent = make_agent(["current_time", "log_note"])

    outcome = await dispatcher.dispatch(
        agent, ToolCall(tool="log_note", args={"text": "note"}), "owner"
    )

    assert outcome.result is None
    assert outcome.pending is not None
    assert outcome.pending.tier == CapabilityTier.REVERSIBLE_WRITE
    # Gated means not executed: nothing was written.
    assert await store.query(Collections.OBSERVATIONS) == []


async def test_dispatcher_auto_executes_log_note_when_policy_allows():
    adapter, store, clock = make_adapter()
    dispatcher = make_dispatcher(adapter, store, clock, auto_approve_reversible=True)
    agent = make_agent(["log_note"])

    outcome = await dispatcher.dispatch(
        agent, ToolCall(tool="log_note", args={"text": "note"}), "owner"
    )

    assert outcome.pending is None
    assert outcome.result is not None and outcome.result.ok is True
    assert len(await store.query(Collections.OBSERVATIONS)) == 1
