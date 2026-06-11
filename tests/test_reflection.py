"""Tests for alfred.domain.reflection: the periodic strategy review."""

from __future__ import annotations

from typing import Any

from alfred.domain.reflection import ReflectionEngine
from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.schemas import (
    AdherenceStats,
    AgentManifest,
    Collections,
    Lifecycle,
    Outcome,
    OutcomeStatus,
    Reflection,
    UserProfile,
)
from alfred.domain.user_model import UserModelService
from alfred.testing import FakeClock, FakeModel, MemoryStore


def make_agent(name: str, lifecycle: Lifecycle = Lifecycle.ESTABLISHED) -> LoadedAgent:
    manifest = AgentManifest(name=name, description="test agent", lifecycle=lifecycle)
    return LoadedAgent(manifest=manifest, prompt="be useful")


def make_engine(
    model: FakeModel, clock: FakeClock | None = None
) -> tuple[ReflectionEngine, MemoryStore, FakeClock, UserModelService]:
    store = MemoryStore()
    clock = clock or FakeClock()
    user_model = UserModelService(store, clock)
    engine = ReflectionEngine(model, user_model, store, clock)
    return engine, store, clock, user_model


def reflection_json(**kwargs: Any) -> str:
    return Reflection.model_validate(kwargs).model_dump_json()


def user_content(model: FakeModel) -> str:
    messages = model.calls[0]["messages"]
    users = [m.content for m in messages if m.role == "user"]
    assert users
    return users[-1]


# ---------------------------------------------------------------------------
# Persistence of the reflection itself
# ---------------------------------------------------------------------------


async def test_insights_are_persisted_and_stamped():
    model = FakeModel([reflection_json(insights=["mornings worked, evenings did not"])])
    engine, store, clock, _ = make_engine(model)

    result = await engine.reflect(AgentRegistry(), window_days=7)

    assert result.insights == ["mornings worked, evenings did not"]
    assert result.window_days == 7
    assert result.created_at == clock.now()
    doc = await store.get(Collections.REFLECTIONS, result.id)
    assert doc is not None
    assert doc["insights"] == ["mornings worked, evenings did not"]


# ---------------------------------------------------------------------------
# Profile updates
# ---------------------------------------------------------------------------


async def test_profile_updates_append_notes_capped_and_recorded():
    updates = ["fact one", "fact two", "fact three"]
    model = FakeModel([reflection_json(profile_updates=updates)])
    engine, store, _, user_model = make_engine(model)
    seeded = UserProfile(notes=[f"old note {i}" for i in range(29)])
    await user_model.save_profile(seeded)

    await engine.reflect(AgentRegistry())

    profile = await user_model.get_profile()
    assert len(profile.notes) == 30
    assert profile.notes[-3:] == updates
    # The oldest notes fall off; recent ones survive.
    assert "old note 0" not in profile.notes
    assert "old note 28" in profile.notes
    observations = await store.query(Collections.OBSERVATIONS)
    assert len(observations) == 3
    assert all(o["source"] == "reflection" for o in observations)
    assert all(o["kind"] == "insight" for o in observations)


async def test_no_profile_updates_means_no_profile_save():
    model = FakeModel([reflection_json()])
    engine, _, _, user_model = make_engine(model)
    before = await user_model.get_profile()

    await engine.reflect(AgentRegistry())

    after = await user_model.get_profile()
    assert after.version == before.version


# ---------------------------------------------------------------------------
# Lifecycle deltas become proposals, never silent edits
# ---------------------------------------------------------------------------


async def test_lifecycle_delta_creates_pending_proposal_without_applying():
    model = FakeModel([reflection_json()])
    engine, store, _, user_model = make_engine(model)
    registry = AgentRegistry([make_agent("runner", lifecycle=Lifecycle.FORMING)])
    profile = UserProfile(adherence={"runner": AdherenceStats(done=14)})
    await user_model.save_profile(profile)

    result = await engine.reflect(registry)

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.kind == "lifecycle_change"
    assert proposal.agent == "runner"
    assert proposal.old == "forming"
    assert proposal.new == "established"
    assert proposal.status == "pending"
    doc = await store.get(Collections.PROPOSALS, proposal.id)
    assert doc is not None and doc["status"] == "pending"
    # The registry and manifest are untouched: only the owner applies changes.
    agent = registry.get("runner")
    assert agent is not None
    assert agent.manifest.lifecycle == Lifecycle.FORMING
    # The persisted reflection carries the proposal too.
    stored = await store.get(Collections.REFLECTIONS, result.id)
    assert stored is not None and len(stored["proposals"]) == 1


async def test_stable_lifecycle_creates_no_proposal():
    model = FakeModel([reflection_json()])
    engine, store, _, _ = make_engine(model)
    registry = AgentRegistry([make_agent("runner", lifecycle=Lifecycle.ESTABLISHED)])

    result = await engine.reflect(registry)

    assert result.proposals == []
    assert await store.query(Collections.PROPOSALS) == []


# ---------------------------------------------------------------------------
# Model-returned proposals
# ---------------------------------------------------------------------------


async def test_model_returned_proposals_are_persisted_forced_pending():
    model = FakeModel(
        [
            reflection_json(
                proposals=[
                    {
                        "kind": "prompt_change",
                        "agent": "runner",
                        "summary": "tighten the prompt",
                        "status": "approved",  # the model does not get a vote
                    }
                ]
            )
        ]
    )
    engine, store, clock, _ = make_engine(model)

    result = await engine.reflect(AgentRegistry())

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.status == "pending"
    assert proposal.created_at == clock.now()
    doc = await store.get(Collections.PROPOSALS, proposal.id)
    assert doc is not None and doc["status"] == "pending"


# ---------------------------------------------------------------------------
# What the model is shown
# ---------------------------------------------------------------------------


async def test_recent_outcomes_appear_in_the_model_prompt():
    model = FakeModel([reflection_json()])
    engine, _, _, user_model = make_engine(model)
    await user_model.record_outcome(Outcome(agent="runner", status=OutcomeStatus.DONE))
    await user_model.record_outcome(
        Outcome(agent="scholar", status=OutcomeStatus.MISSED, report="exam week")
    )

    await engine.reflect(AgentRegistry(), window_days=7)

    content = user_content(model)
    assert "runner" in content
    assert "scholar" in content
    assert "exam week" in content


async def test_outcomes_outside_the_window_are_excluded():
    model = FakeModel([reflection_json()])
    engine, _, clock, user_model = make_engine(model)
    await user_model.record_outcome(Outcome(agent="ancient", status=OutcomeStatus.DONE))
    clock.advance(days=10)
    await user_model.record_outcome(Outcome(agent="recent", status=OutcomeStatus.DONE))

    await engine.reflect(AgentRegistry(), window_days=7)

    content = user_content(model)
    assert "recent" in content
    assert "ancient" not in content
