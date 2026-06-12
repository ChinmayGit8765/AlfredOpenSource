"""Cohesion tests: memories and peer plans reach every agent's brief,
the owner's memory commands work, and outbound routing picks the right
transport by channel namespace."""

from __future__ import annotations

import json
from datetime import date

from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.executor import AgentExecutor
from alfred.domain.governance import PendingActions, Policy
from alfred.domain.memory import MemoryService
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import AgentManifest, Collections, Plan, PlanItem
from alfred.domain.user_model import UserModelService
from alfred.ports.transport import OutboundMessage
from alfred.runtime.composition import MultiTransport
from alfred.testing import CapturingTransport, FakeClock, FakeModel, FakeTools, MemoryStore


def reply_json(text: str = "ok") -> str:
    return json.dumps(
        {"reply": text, "plan": None, "tool_calls": [], "observations": [], "done": True}
    )


def make_agent(name: str = "training") -> LoadedAgent:
    return LoadedAgent(
        manifest=AgentManifest(name=name, description=f"{name} agent"),
        prompt=f"You are the {name} agent.",
    )


def make_executor(
    model: FakeModel,
) -> tuple[AgentExecutor, MemoryStore, MemoryService, FakeClock]:
    store = MemoryStore()
    clock = FakeClock()
    tools = FakeTools()
    user_model = UserModelService(store, clock)
    memory = MemoryService(store, clock)
    pending = PendingActions(store, clock)
    dispatcher = ToolDispatcher(tools, store, clock, Policy(), pending)
    executor = AgentExecutor(
        model, tools, dispatcher, user_model, store, clock, memory=memory
    )
    return executor, store, memory, clock


async def test_relevant_memories_reach_the_agent_prompt() -> None:
    model = FakeModel([reply_json()])
    executor, _, memory, _ = make_executor(model)
    await memory.remember("Physio said no overhead pressing until March")
    await memory.remember("Rent is due on the 3rd")

    await executor.run(
        make_agent(), text="plan my pressing for this week", provenance="owner"
    )

    system = model.calls[0]["messages"][0].content
    assert "overhead pressing" in system
    assert "Rent" not in system  # irrelevant memories stay out of the brief


async def test_irrelevant_message_gets_no_memory_block() -> None:
    model = FakeModel([reply_json()])
    executor, _, memory, _ = make_executor(model)
    await memory.remember("Physio said no overhead pressing until March")

    await executor.run(make_agent(), text="how was the climbing gym", provenance="owner")

    system = model.calls[0]["messages"][0].content
    assert "Relevant things the owner has told you" not in system


async def test_peer_plans_for_current_week_reach_the_prompt() -> None:
    model = FakeModel([reply_json()])
    executor, store, _, clock = make_executor(model)
    today = clock.now().date()
    week_of = today.fromordinal(today.toordinal() - today.weekday())
    peer_plan = Plan(
        agent="study",
        week_of=week_of,
        items=[PlanItem(title="Past paper FIT3170", day="tue", load=3)],
    )
    await store.append(Collections.PLANS, peer_plan.model_dump(mode="json"))
    stale = Plan(
        agent="build",
        week_of=date(2020, 1, 6),
        items=[PlanItem(title="ancient task", load=1)],
    )
    await store.append(Collections.PLANS, stale.model_dump(mode="json"))

    await executor.run(make_agent("training"), text="plan my week", provenance="owner")

    system = model.calls[0]["messages"][0].content
    assert "study" in system and "Past paper FIT3170" in system
    assert "ancient task" not in system  # other weeks are not this week's load


async def test_owner_memory_commands_end_to_end(tmp_path) -> None:
    from pathlib import Path

    from alfred.config import AlfredConfig
    from alfred.domain.schemas import InboundMessage
    from alfred.runtime.composition import build_system

    repo_agents = Path(__file__).resolve().parent.parent / "agents"
    transport = CapturingTransport()
    config = AlfredConfig(data_dir=tmp_path / "data", agents_dir=repo_agents)
    system = build_system(config, fake=True, transport=transport)

    async def say(text: str) -> str:
        await system.core.handle_inbound(InboundMessage(channel="cli", text=text))
        return transport.sent[-1].text

    filed = await say("remember physio said no overhead pressing until March")
    assert "Filed" in filed
    memory_id = filed.split("(")[1].split(")")[0]

    recalled = await say("what do you know about overhead pressing?")
    assert "no overhead pressing" in recalled

    listed = await say("memories")
    assert memory_id in listed

    forgotten = await say(f"forget {memory_id}")
    assert "Forgotten" in forgotten
    assert "Nothing filed" in await say("recall overhead pressing")


async def test_multitransport_routes_by_namespace_and_drops_unroutable() -> None:
    discord = CapturingTransport()
    telegram = CapturingTransport()
    multi = MultiTransport({"discord": discord, "telegram": telegram})

    await multi.send(OutboundMessage(channel="telegram:42", text="t"))
    await multi.send(OutboundMessage(channel="discord:99", text="d"))
    await multi.send(OutboundMessage(channel="12345", text="bare legacy id"))
    await multi.send(OutboundMessage(channel="carrierpigeon:1", text="lost"))

    assert [m.text for m in telegram.sent] == ["t"]
    assert [m.text for m in discord.sent] == ["d", "bare legacy id"]
