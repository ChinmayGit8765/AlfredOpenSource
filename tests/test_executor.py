"""Tests for alfred.domain.executor: one governed agent run."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.executor import AgentExecutor
from alfred.domain.governance import PendingActions, Policy
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import (
    AgentManifest,
    AgentReply,
    Collections,
    Plan,
    PlanItem,
    ToolCall,
)
from alfred.domain.user_model import UserModelService
from alfred.ports.model import ModelMessage, ModelOptions
from alfred.ports.tools import CapabilityTier
from alfred.testing import FakeClock, FakeModel, FakeTools, MemoryStore

READ_TOOL = "current_time"
DESTRUCTIVE_TOOL = "delete_file"


def make_agent(
    name: str = "trainer",
    allowed: list[str] | None = None,
    model: ModelOptions | None = None,
) -> LoadedAgent:
    manifest = AgentManifest(
        name=name,
        description="test agent",
        allowed_tools=allowed or [],
        model=model,
    )
    return LoadedAgent(manifest=manifest, prompt="You are the training agent.")


def make_executor(
    model: FakeModel,
    tools: FakeTools | None = None,
    clock: FakeClock | None = None,
) -> tuple[AgentExecutor, MemoryStore, FakeClock, FakeTools]:
    tools = tools or FakeTools()
    store = MemoryStore()
    clock = clock or FakeClock()
    pending = PendingActions(store, clock)
    dispatcher = ToolDispatcher(tools, store, clock, Policy(), pending)
    user_model = UserModelService(store, clock)
    executor = AgentExecutor(model, tools, dispatcher, user_model, store, clock)
    return executor, store, clock, tools


def reply_json(**kwargs: Any) -> str:
    return AgentReply.model_validate({"reply": "ok", **kwargs}).model_dump_json()


def tool_messages(call: dict[str, Any]) -> list[str]:
    return [
        m.content
        for m in call["messages"]
        if isinstance(m, ModelMessage) and m.role == "tool"
    ]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_reply_only_happy_path():
    model = FakeModel([reply_json(reply="hello owner")])
    executor, store, _, _ = make_executor(model)

    result = await executor.run(make_agent(), text="hi", provenance="owner")

    assert result.agent == "trainer"
    assert result.replies == ["hello owner"]
    assert result.plan is None
    assert result.pending == []
    assert result.observations == []
    assert len(model.calls) == 1
    runs = [
        r for r in await store.query(Collections.AUDIT) if r["event"] == "agent_run"
    ]
    assert len(runs) == 1
    assert runs[0]["agent"] == "trainer"
    assert runs[0]["provenance"] == "owner"
    assert runs[0]["rounds"] == 1
    assert runs[0]["tool_calls"] == 0


async def test_plan_is_stamped_persisted_and_week_of_defaults_to_monday():
    plan = Plan(items=[PlanItem(title="run 5k", day="wed", load=2)])
    model = FakeModel([reply_json(reply="here is your plan", plan=plan.model_dump(mode="json"))])
    # A Wednesday; the Monday of that week is 2026-01-05.
    clock = FakeClock(datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc))
    executor, store, _, _ = make_executor(model, clock=clock)

    result = await executor.run(make_agent(), text="plan my week", provenance="owner")

    assert result.plan is not None
    assert result.plan.agent == "trainer"
    assert result.plan.created_at == clock.now()
    assert result.plan.week_of == date(2026, 1, 5)
    # Plans are appended under time-ordered store keys (newest_first works),
    # so the lookup is a query by the plan's own id field, not a keyed get.
    docs = await store.query(Collections.PLANS, where={"id": result.plan.id})
    assert len(docs) == 1
    doc = docs[0]
    assert doc["agent"] == "trainer"
    assert doc["week_of"] == "2026-01-05"
    runs = [
        r for r in await store.query(Collections.AUDIT) if r["event"] == "agent_run"
    ]
    assert runs[0]["plan_id"] == result.plan.id


async def test_explicit_week_of_is_preserved():
    plan = Plan(week_of=date(2026, 2, 2), items=[PlanItem(title="read")])
    model = FakeModel([reply_json(plan=plan.model_dump(mode="json"))])
    executor, _, _, _ = make_executor(model)

    result = await executor.run(make_agent(), text="plan", provenance="owner")

    assert result.plan is not None
    assert result.plan.week_of == date(2026, 2, 2)


async def test_observations_are_recorded():
    model = FakeModel(
        [reply_json(reply="noted", observations=["prefers morning sessions"])]
    )
    executor, store, _, _ = make_executor(model)

    result = await executor.run(make_agent(), text="hi", provenance="owner")

    assert result.observations == ["prefers morning sessions"]
    docs = await store.query(Collections.OBSERVATIONS)
    assert len(docs) == 1
    assert docs[0]["source"] == "trainer"
    assert docs[0]["kind"] == "insight"
    assert docs[0]["text"] == "prefers morning sessions"


# ---------------------------------------------------------------------------
# Tool rounds
# ---------------------------------------------------------------------------


async def test_read_only_tool_round_trip():
    tools = FakeTools()
    tools.add(
        READ_TOOL,
        tier=CapabilityTier.READ_ONLY,
        handler=lambda **kwargs: {"time": "09:00"},
    )
    model = FakeModel(
        [
            reply_json(
                reply="checking the time",
                done=False,
                tool_calls=[{"tool": READ_TOOL, "args": {}}],
            ),
            reply_json(reply="it is 09:00", done=True),
        ]
    )
    executor, _, _, _ = make_executor(model, tools=tools)
    agent = make_agent(allowed=[READ_TOOL])

    result = await executor.run(agent, text="what time is it", provenance="owner")

    assert result.replies == ["checking the time", "it is 09:00"]
    assert len(model.calls) == 2
    assert tools.invocations == [(READ_TOOL, {})]
    fed_back = tool_messages(model.calls[1])
    assert any(READ_TOOL in m and '"time":"09:00"' in m.replace(" ", "") for m in fed_back)


async def test_destructive_tool_is_gated_not_invoked():
    tools = FakeTools()
    tools.add(DESTRUCTIVE_TOOL, tier=CapabilityTier.DESTRUCTIVE)
    model = FakeModel(
        [
            reply_json(
                reply="I need to delete that file",
                done=False,
                tool_calls=[{"tool": DESTRUCTIVE_TOOL, "args": {"path": "x"}}],
            ),
            reply_json(reply="awaiting your confirmation", done=True),
        ]
    )
    executor, _, _, _ = make_executor(model, tools=tools)
    agent = make_agent(allowed=[DESTRUCTIVE_TOOL])

    result = await executor.run(agent, text="clean up", provenance="owner")

    assert len(result.pending) == 1
    assert result.pending[0].status == "pending"
    assert tools.invocations == []
    fed_back = tool_messages(model.calls[1])
    assert any("confirmation" in m for m in fed_back)
    assert any(result.pending[0].id in m for m in fed_back)


async def test_tool_not_in_allowlist_feeds_back_refusal_without_crashing():
    tools = FakeTools()
    tools.add(READ_TOOL, tier=CapabilityTier.READ_ONLY)
    model = FakeModel(
        [
            reply_json(
                reply="let me check",
                done=False,
                tool_calls=[{"tool": READ_TOOL, "args": {}}],
            ),
            reply_json(reply="I cannot use that tool", done=True),
        ]
    )
    executor, store, _, _ = make_executor(model, tools=tools)
    agent = make_agent(allowed=[])  # nothing allowed

    result = await executor.run(agent, text="check", provenance="owner")

    assert result.replies[-1] == "I cannot use that tool"
    assert result.pending == []
    assert tools.invocations == []
    fed_back = tool_messages(model.calls[1])
    assert any("refused" in m for m in fed_back)
    events = [r["event"] for r in await store.query(Collections.AUDIT)]
    assert "tool_denied" in events


async def test_unknown_tool_feeds_back_refusal_without_crashing():
    model = FakeModel(
        [
            reply_json(
                done=False, tool_calls=[{"tool": "ghost_tool", "args": {}}]
            ),
            reply_json(reply="that tool does not exist", done=True),
        ]
    )
    executor, store, _, tools = make_executor(model)
    agent = make_agent(allowed=["ghost_tool"])

    result = await executor.run(agent, text="go", provenance="owner")

    assert result.replies[-1] == "that tool does not exist"
    assert tools.invocations == []
    assert any("refused" in m for m in tool_messages(model.calls[1]))
    events = [r["event"] for r in await store.query(Collections.AUDIT)]
    assert "tool_not_found" in events


# ---------------------------------------------------------------------------
# Round limits and options
# ---------------------------------------------------------------------------


async def test_max_rounds_is_respected_and_note_appended():
    tools = FakeTools()
    tools.add(READ_TOOL, tier=CapabilityTier.READ_ONLY)
    # One scripted response repeats forever: always done=False with a call.
    model = FakeModel(
        [
            reply_json(
                reply="still working",
                done=False,
                tool_calls=[{"tool": READ_TOOL, "args": {}}],
            )
        ]
    )
    executor, store, _, _ = make_executor(model, tools=tools)
    agent = make_agent(allowed=[READ_TOOL])

    result = await executor.run(agent, text="go", provenance="owner", max_rounds=2)

    assert len(model.calls) == 2
    assert len(tools.invocations) == 2
    assert "round limit" in result.replies[-1]
    runs = [
        r for r in await store.query(Collections.AUDIT) if r["event"] == "agent_run"
    ]
    assert runs[0]["rounds"] == 2


async def test_done_false_without_tool_feedback_stops_early():
    model = FakeModel([reply_json(reply="hmm", done=False)])
    executor, _, _, _ = make_executor(model)

    result = await executor.run(make_agent(), text="go", provenance="owner")

    # No tool messages means nothing new for another round.
    assert len(model.calls) == 1
    assert "round limit" in result.replies[-1]


async def test_manifest_model_options_are_forwarded():
    options = ModelOptions(model="qwen3:8b", temperature=0.1)
    model = FakeModel([reply_json()])
    executor, _, _, _ = make_executor(model)
    agent = make_agent(model=options)

    await executor.run(agent, text="hi", provenance="owner")

    assert model.calls[0]["options"] == options


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


async def test_system_prompt_contains_agent_prompt_governance_and_tool_specs():
    tools = FakeTools()
    tools.add(READ_TOOL, tier=CapabilityTier.READ_ONLY, description="current time")
    tools.add("other_tool", tier=CapabilityTier.READ_ONLY)
    model = FakeModel([reply_json()])
    executor, _, _, _ = make_executor(model, tools=tools)
    agent = make_agent(allowed=[READ_TOOL])

    await executor.run(agent, text="hi", provenance="owner")

    system = model.calls[0]["messages"][0]
    assert system.role == "system"
    assert "You are the training agent." in system.content
    assert "owner confirmation" in system.content
    assert "Owner profile" in system.content
    assert READ_TOOL in system.content
    assert "read_only" in system.content
    # Tools outside the allowlist are never advertised.
    assert "other_tool" not in system.content
    assert "AgentReply" in system.content
