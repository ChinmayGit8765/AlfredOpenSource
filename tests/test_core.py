"""AlfredCore tests: commands, routing, governance flows, builder, conductor.

Everything runs against the real in-memory fakes; the only scripted part
is the model. One smoke test goes through build_system(fake=True) to
prove the composition root assembles a working system.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from alfred.config import AlfredConfig
from alfred.domain.builder import AgentBuilder
from alfred.domain.conductor import Conductor
from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.executor import AgentExecutor
from alfred.domain.governance import PendingActions, Policy, Proposals
from alfred.domain.reflection import ReflectionEngine
from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.schemas import (
    AgentManifest,
    BuilderStage,
    Collections,
    InboundMessage,
    Lifecycle,
    Proposal,
    ProposalKind,
    ScheduledTrigger,
    TargetShape,
    Triggers,
)
from alfred.domain.user_model import UserModelService
from alfred.ports.tools import CapabilityTier
from alfred.runtime.composition import build_system
from alfred.runtime.core import AlfredCore
from alfred.testing.fakes import CapturingTransport, FakeClock, FakeModel, FakeTools, MemoryStore

REPO_AGENTS_DIR = Path(__file__).parent.parent / "agents"

TRAINING_TOOLS = ["current_time", "list_plans", "list_recent_outcomes", "log_note"]


def make_agent(
    name: str,
    keywords: list[str],
    *,
    allowed_tools: list[str] | None = None,
    shape: TargetShape | None = TargetShape.SKILL,
) -> LoadedAgent:
    manifest = AgentManifest(
        name=name,
        description=f"{name} test agent",
        shape=shape,
        lifecycle=Lifecycle.ESTABLISHED,
        triggers=Triggers(keywords=keywords),
        allowed_tools=allowed_tools or [],
    )
    return LoadedAgent(manifest=manifest, prompt=f"You are the {name} agent.")


def agent_reply(
    text: str,
    *,
    plan: dict | None = None,
    tool_calls: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "reply": text,
            "plan": plan,
            "tool_calls": tool_calls or [],
            "observations": [],
            "done": True,
        }
    )


@dataclass
class World:
    core: AlfredCore
    transport: CapturingTransport
    store: MemoryStore
    tools: FakeTools
    pending: PendingActions
    proposals: Proposals
    registry: AgentRegistry
    user_model: UserModelService
    model: FakeModel
    builder: AgentBuilder
    agents_dir: Path


def make_world(
    tmp_path: Path,
    model: FakeModel,
    agents: list[LoadedAgent],
    tools: FakeTools | None = None,
) -> World:
    store = MemoryStore()
    clock = FakeClock()
    tools = tools or FakeTools()
    transport = CapturingTransport()
    registry = AgentRegistry(agents)
    user_model = UserModelService(store, clock)
    pending = PendingActions(store, clock)
    proposals = Proposals(store, clock)
    dispatcher = ToolDispatcher(tools, store, clock, Policy(), pending)
    executor = AgentExecutor(model, tools, dispatcher, user_model, store, clock)
    conductor = Conductor(model, clock)
    builder = AgentBuilder(model, user_model, store, clock)
    reflection = ReflectionEngine(model, user_model, store, clock)
    agents_dir = tmp_path / "agents"
    config = AlfredConfig(data_dir=tmp_path / "data", agents_dir=agents_dir)
    core = AlfredCore(
        registry,
        executor,
        conductor,
        builder,
        user_model,
        dispatcher,
        pending,
        proposals,
        reflection,
        store,
        clock,
        transport,
        config,
        agents_dir,
    )
    return World(
        core=core,
        transport=transport,
        store=store,
        tools=tools,
        pending=pending,
        proposals=proposals,
        registry=registry,
        user_model=user_model,
        model=model,
        builder=builder,
        agents_dir=agents_dir,
    )


def inbound(text: str) -> InboundMessage:
    return InboundMessage(channel="cli", text=text)


def sent_texts(world: World) -> list[str]:
    return [m.text for m in world.transport.sent]


async def test_help_and_status_reply(tmp_path: Path) -> None:
    world = make_world(tmp_path, FakeModel(), [make_agent("training", ["train"])])

    await world.core.handle_inbound(inbound("help"))
    await world.core.handle_inbound(inbound("STATUS"))

    texts = sent_texts(world)
    assert len(texts) == 2
    assert "ALFRED commands" in texts[0]
    assert "confirm <id>" in texts[0]
    assert "training" in texts[1]
    assert "established" in texts[1]
    assert "Pending actions: 0" in texts[1]


async def test_unrouted_text_falls_back_and_mentions_agents(tmp_path: Path) -> None:
    world = make_world(tmp_path, FakeModel(), [make_agent("training", ["train", "gym"])])

    await world.core.handle_inbound(inbound("hello there"))

    texts = sent_texts(world)
    assert len(texts) == 1
    assert "ALFRED" in texts[0]
    assert "training" in texts[0]
    assert "train" in texts[0]
    assert "help" in texts[0]


async def test_routed_message_replies_and_persists_plan(tmp_path: Path) -> None:
    plan = {
        "items": [{"title": "Squat 3x5", "day": "mon", "load": 2}],
        "rationale": "base week",
    }
    model = FakeModel([agent_reply("Here is the week.", plan=plan)])
    world = make_world(
        tmp_path, model, [make_agent("training", ["train"], allowed_tools=TRAINING_TOOLS)]
    )

    await world.core.handle_inbound(inbound("train me for next week"))

    assert any("Here is the week." in t for t in sent_texts(world))
    docs = await world.store.query(Collections.PLANS)
    assert len(docs) == 1
    assert docs[0]["agent"] == "training"
    assert docs[0]["items"][0]["title"] == "Squat 3x5"


async def test_destructive_tool_gates_then_confirm_executes(tmp_path: Path) -> None:
    tools = FakeTools()
    tools.add(
        "delete_event",
        tier=CapabilityTier.DESTRUCTIVE,
        handler=lambda **kwargs: {"deleted": True},
    )
    model = FakeModel(
        [
            agent_reply(
                "I need your sign-off to delete that.",
                tool_calls=[
                    {
                        "tool": "delete_event",
                        "args": {"event": "standup"},
                        "reason": "owner asked to clear it",
                    }
                ],
            )
        ]
    )
    world = make_world(
        tmp_path,
        model,
        [make_agent("training", ["train"], allowed_tools=["delete_event"])],
        tools=tools,
    )

    await world.core.handle_inbound(inbound("train: clear my standup"))

    actions = await world.pending.list_pending()
    assert len(actions) == 1
    action = actions[0]
    assert world.tools.invocations == []
    assert any(action.id in t and "delete_event" in t for t in sent_texts(world))

    await world.core.handle_inbound(inbound(f"confirm {action.id}"))

    assert world.tools.invocations == [("delete_event", {"event": "standup"})]
    assert any(f"Confirmed {action.id}" in t for t in sent_texts(world))
    resolved = await world.pending.get(action.id)
    assert resolved is not None
    assert resolved.status == "confirmed"


async def test_deny_rejects_pending_action(tmp_path: Path) -> None:
    tools = FakeTools()
    tools.add("delete_event", tier=CapabilityTier.DESTRUCTIVE)
    model = FakeModel(
        [
            agent_reply(
                "Awaiting your call.",
                tool_calls=[{"tool": "delete_event", "args": {}, "reason": "cleanup"}],
            )
        ]
    )
    world = make_world(
        tmp_path,
        model,
        [make_agent("training", ["train"], allowed_tools=["delete_event"])],
        tools=tools,
    )

    await world.core.handle_inbound(inbound("train: tidy my calendar"))
    action = (await world.pending.list_pending())[0]

    await world.core.handle_inbound(inbound(f"deny {action.id}"))

    assert world.tools.invocations == []
    resolved = await world.pending.get(action.id)
    assert resolved is not None
    assert resolved.status == "rejected"
    assert any(f"Denied {action.id}" in t for t in sent_texts(world))


async def test_outcome_shorthand_records_outcome(tmp_path: Path) -> None:
    model = FakeModel([agent_reply("Nice work; logged.")])
    world = make_world(
        tmp_path,
        model,
        [make_agent("training", ["train", "training"], allowed_tools=TRAINING_TOOLS)],
    )

    await world.core.handle_inbound(inbound("done with training"))

    docs = await world.store.query(Collections.OUTCOMES)
    assert len(docs) == 1
    assert docs[0]["agent"] == "training"
    assert docs[0]["status"] == "done"
    assert docs[0]["report"] == "done with training"
    assert any("Logged outcome 'done' for training" in t for t in sent_texts(world))
    # The agent still runs after the outcome is recorded.
    assert any("Nice work; logged." in t for t in sent_texts(world))


async def test_builder_flow_starts_and_steps(tmp_path: Path) -> None:
    model = FakeModel(
        [
            json.dumps(
                {
                    "question": "When does reading actually fail in your day?",
                    "satisfied": False,
                    "real_lever": None,
                }
            ),
            json.dumps(
                {
                    "question": "",
                    "satisfied": True,
                    "real_lever": "get off the phone at night",
                }
            ),
            json.dumps({"shape": "habit", "rationale": "recurring nightly behaviour"}),
        ]
    )
    world = make_world(tmp_path, model, [make_agent("training", ["train"])])

    await world.core.handle_inbound(inbound("new agent read more"))
    assert any("When does reading actually fail" in t for t in sent_texts(world))

    await world.core.handle_inbound(inbound("evenings; I doomscroll instead"))

    assert any("get off the phone at night" in t for t in sent_texts(world))
    session = await world.builder.active_session()
    assert session is not None
    assert session.stage is BuilderStage.DESIGNING


async def test_approve_touches_safety_requires_suffix(tmp_path: Path) -> None:
    world = make_world(tmp_path, FakeModel(), [make_agent("training", ["train"])])
    proposal = await world.proposals.create(
        Proposal(
            kind=ProposalKind.MANIFEST_CHANGE,
            agent="training",
            summary="Widen the tool allowlist",
            touches_safety=True,
        )
    )

    await world.core.handle_inbound(inbound(f"approve {proposal.id}"))

    doc = await world.store.get(Collections.PROPOSALS, proposal.id)
    assert doc is not None
    assert doc["status"] == "pending"
    assert any("confirm-safety" in t for t in sent_texts(world))

    await world.core.handle_inbound(inbound(f"approve {proposal.id} confirm-safety"))

    doc = await world.store.get(Collections.PROPOSALS, proposal.id)
    assert doc is not None
    assert doc["status"] == "approved"
    assert any(f"Approved proposal {proposal.id}" in t for t in sent_texts(world))


async def test_two_plans_invoke_conductor_and_persist_schedule(tmp_path: Path) -> None:
    study_plan = {
        "items": [{"title": "Revise algorithms", "day": "mon", "load": 1}],
        "rationale": "exam prep",
    }
    training_plan = {
        "items": [{"title": "Easy run", "day": "tue", "load": 1}],
        "rationale": "base week",
    }
    # Routed order is alphabetical: study first, then training. The plans
    # do not conflict, so reconcile is a deterministic passthrough and no
    # model call is needed for the conductor itself.
    model = FakeModel(
        [
            agent_reply("Study plan ready.", plan=study_plan),
            agent_reply("Training plan ready.", plan=training_plan),
        ]
    )
    world = make_world(
        tmp_path,
        model,
        [
            make_agent("study", ["plan"], allowed_tools=TRAINING_TOOLS),
            make_agent("training", ["plan"], allowed_tools=TRAINING_TOOLS),
        ],
    )

    await world.core.handle_inbound(inbound("plan my week"))

    texts = sent_texts(world)
    assert any("Study plan ready." in t for t in texts)
    assert any("Training plan ready." in t for t in texts)
    assert any("no conflicts" in t for t in texts)

    # The reconciled schedule lives in its own collection, never in PLANS,
    # where it would pollute every Plan query.
    schedules = await world.store.query(Collections.SCHEDULES)
    assert len(schedules) == 1
    assert len(schedules[0]["plans"]) == 2
    plan_docs = await world.store.query(Collections.PLANS)
    assert all("plans" not in d for d in plan_docs)


async def test_executor_failure_surfaces_apology(tmp_path: Path) -> None:
    model = FakeModel(["this is not json at all"])
    world = make_world(
        tmp_path, model, [make_agent("training", ["train"], allowed_tools=TRAINING_TOOLS)]
    )

    await world.core.handle_inbound(inbound("train hard this week"))

    texts = sent_texts(world)
    assert len(texts) == 1
    assert "StructuredCallError" in texts[0]
    assert "Traceback" not in texts[0]


async def test_alfred_stop_sets_flag(tmp_path: Path) -> None:
    world = make_world(tmp_path, FakeModel(), [make_agent("training", ["train"])])

    await world.core.handle_inbound(inbound("alfred stop"))

    assert world.core.stop_requested is True
    assert any("shutting down" in t.lower() for t in sent_texts(world))


async def test_run_scheduled_delivers_to_last_owner_channel(tmp_path: Path) -> None:
    model = FakeModel(
        [agent_reply("On it."), agent_reply("Scheduled plan delivered.")]
    )
    world = make_world(
        tmp_path, model, [make_agent("training", ["train"], allowed_tools=TRAINING_TOOLS)]
    )

    # The owner speaks first; scheduled output then follows that channel.
    await world.core.handle_inbound(inbound("train tomorrow"))
    world.transport.sent.clear()

    await world.core.run_scheduled(ScheduledTrigger(agent="training", reason="schedule"))

    assert world.transport.sent
    assert world.transport.sent[0].channel == "cli"
    assert "Scheduled plan delivered." in world.transport.sent[0].text


async def test_run_scheduled_with_no_known_channel_drops_loudly(tmp_path: Path) -> None:
    # No configured channel and the owner has never messaged: the job still
    # runs (the plan persists) but nothing is sent into the void.
    model = FakeModel([agent_reply("Scheduled plan delivered.")])
    world = make_world(
        tmp_path, model, [make_agent("training", ["train"], allowed_tools=TRAINING_TOOLS)]
    )

    await world.core.run_scheduled(ScheduledTrigger(agent="training", reason="schedule"))

    assert world.transport.sent == []


async def test_build_system_fake_smoke(tmp_path: Path) -> None:
    transport = CapturingTransport()
    config = AlfredConfig(data_dir=tmp_path / "data", agents_dir=REPO_AGENTS_DIR)
    system = build_system(config, fake=True, transport=transport)

    assert {a.manifest.name for a in system.registry.all()} == {
        "build",
        "study",
        "training",
    }

    await system.core.handle_inbound(inbound("status"))

    assert transport.sent
    assert "training" in transport.sent[0].text
