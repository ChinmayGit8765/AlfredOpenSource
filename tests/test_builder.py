"""Tests for the Adaptive Agent Builder and its WIP guardrail."""

from __future__ import annotations

import json

from alfred.domain.builder import AgentBuilder, check_wip
from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.schemas import (
    AgentManifest,
    BuilderStage,
    Collections,
    Lifecycle,
    TargetShape,
    UserProfile,
)
from alfred.testing import FakeClock, FakeModel, MemoryStore


class StubUserModel:
    """Profile source for the builder; it only ever reads the profile.

    The real UserModelService lives in alfred.domain.user_model (built in
    parallel); this stand-in keeps these tests independent of it.
    """

    def __init__(self, profile: UserProfile | None = None) -> None:
        self._profile = profile or UserProfile()

    async def get_profile(self) -> UserProfile:
        return self._profile


def make_agent(
    name: str,
    lifecycle: Lifecycle = Lifecycle.ESTABLISHED,
    shape: TargetShape | None = TargetShape.HABIT,
    capacity: int = 2,
) -> LoadedAgent:
    return LoadedAgent(
        manifest=AgentManifest(
            name=name,
            description=f"The {name} agent.",
            lifecycle=lifecycle,
            shape=shape,
            capacity_cost=capacity,
        ),
        prompt=f"You are {name}.",
    )


def make_builder(
    weekly_capacity: int = 20, responses: list[str] | None = None
) -> tuple[AgentBuilder, FakeModel, MemoryStore]:
    model = FakeModel(responses or [])
    store = MemoryStore()
    builder = AgentBuilder(
        model,
        StubUserModel(UserProfile(weekly_capacity=weekly_capacity)),
        store,
        FakeClock(),
    )
    return builder, model, store


ELICIT_Q1 = json.dumps(
    {
        "question": "When during the day do you actually want to read, and what gets in the way?",
        "satisfied": False,
        "real_lever": None,
    }
)
ELICIT_DONE = json.dumps(
    {"question": "", "satisfied": True, "real_lever": "get off the phone at night"}
)
SHAPE_HABIT = json.dumps(
    {"shape": "habit", "rationale": "a recurring nightly behaviour"}
)


def blueprint_json(name: str = "phone-curfew", description: str | None = None) -> str:
    # Deliberately violates the rules the builder must enforce in code:
    # wrong lifecycle, pre-granted tools, oversized cost, no daily schedule.
    return json.dumps(
        {
            "manifest": {
                "name": name,
                "description": description
                or "Protect a phone-free wind-down before bed.",
                "shape": "habit",
                "lifecycle": "established",
                "allowed_tools": ["notify", "calendar"],
                "capacity_cost": 9,
                "schedule": {"kind": "none"},
            },
            "prompt_md": "You help the owner wind down without the phone.",
        }
    )


HAPPY_SCRIPT = [ELICIT_Q1, ELICIT_DONE, SHAPE_HABIT, blueprint_json()]


async def reload(store: MemoryStore, session_id: str) -> dict:
    doc = await store.get(Collections.BUILDER_SESSIONS, session_id)
    assert doc is not None, "session must be persisted on every change"
    return doc


# --- check_wip ----------------------------------------------------------------


def test_check_wip_below_limit() -> None:
    registry = AgentRegistry([make_agent("walk", Lifecycle.FORMING, TargetShape.HABIT)])
    verdict = check_wip(registry)
    assert verdict.allowed is True
    assert verdict.forming_count == 1


def test_check_wip_at_limit_names_the_forming_agents() -> None:
    registry = AgentRegistry(
        [
            make_agent("walk", Lifecycle.FORMING, TargetShape.HABIT),
            make_agent("wind-down", Lifecycle.RESHAPED, TargetShape.STATE),
        ]
    )
    verdict = check_wip(registry)
    assert verdict.allowed is False
    assert verdict.forming_count == 2
    assert "walk" in verdict.detail and "wind-down" in verdict.detail


def test_check_wip_ignores_non_habit_shapes() -> None:
    # A forming SKILL or PROJECT does not draw on the shared willpower budget.
    registry = AgentRegistry(
        [
            make_agent("piano", Lifecycle.FORMING, TargetShape.SKILL),
            make_agent("thesis", Lifecycle.FORMING, TargetShape.PROJECT),
            make_agent("walk", Lifecycle.ESTABLISHED, TargetShape.HABIT),
        ]
    )
    verdict = check_wip(registry)
    assert verdict.allowed is True
    assert verdict.forming_count == 0


def test_check_wip_honours_custom_limit() -> None:
    registry = AgentRegistry([make_agent("walk", Lifecycle.FORMING, TargetShape.HABIT)])
    verdict = check_wip(registry, limit=1)
    assert verdict.allowed is False
    assert "walk" in verdict.detail


# --- happy path ----------------------------------------------------------------


async def test_builder_happy_path_to_done() -> None:
    builder, model, store = make_builder(responses=list(HAPPY_SCRIPT))
    registry = AgentRegistry()

    session, question = await builder.start("read more", registry)
    assert session.stage is BuilderStage.ELICITING
    assert "?" in question
    doc = await reload(store, session.id)
    assert doc["stage"] == "eliciting"
    assert doc["created_at"] is not None and doc["updated_at"] is not None

    session, message = await builder.step(
        session.id, "mostly I doomscroll in bed instead", registry
    )
    assert session.real_lever == "get off the phone at night"
    assert session.shape is TargetShape.HABIT
    assert session.stage is BuilderStage.DESIGNING
    assert "habit" in message.lower()
    doc = await reload(store, session.id)
    assert doc["stage"] == "designing"

    session, message = await builder.step(session.id, "yes, build it", registry)
    assert session.stage is BuilderStage.AWAITING_APPROVAL
    blueprint = session.blueprint
    assert blueprint is not None
    # Enforced in code regardless of what the model emitted:
    assert blueprint.manifest.allowed_tools == []
    assert blueprint.manifest.lifecycle is Lifecycle.PROPOSED
    assert blueprint.manifest.schedule.kind == "daily"
    assert 1 <= blueprint.manifest.capacity_cost <= 4
    prompt = blueprint.prompt_md.lower()
    for marker in ("identity", "scope", "smallest", "anchor", "tone", "output"):
        assert marker in prompt
    assert "phone-curfew" in message
    doc = await reload(store, session.id)
    assert doc["stage"] == "awaiting_approval"
    assert doc["blueprint"]["manifest"]["allowed_tools"] == []
    assert doc["blueprint"]["manifest"]["lifecycle"] == "proposed"

    session, message = await builder.step(session.id, "ship it", registry)
    assert session.stage is BuilderStage.DONE
    assert session.blueprint is not None
    assert session.blueprint.manifest.lifecycle is Lifecycle.FORMING
    doc = await reload(store, session.id)
    assert doc["stage"] == "done"
    assert doc["blueprint"]["manifest"]["lifecycle"] == "forming"
    # Four model calls drove the whole flow; approval needed none.
    assert len(model.calls) == 4


async def test_blueprint_name_collision_gets_suffixed() -> None:
    builder, _, _ = make_builder(responses=list(HAPPY_SCRIPT))
    registry = AgentRegistry(
        [make_agent("phone-curfew", Lifecycle.ESTABLISHED, TargetShape.SKILL, capacity=1)]
    )
    session, _ = await builder.start("read more", registry)
    session, _ = await builder.step(session.id, "I doomscroll at night", registry)
    session, _ = await builder.step(session.id, "go for it", registry)
    assert session.blueprint is not None
    assert session.blueprint.manifest.name == "phone-curfew-2"


# --- refusals -------------------------------------------------------------------


async def test_start_refuses_at_wip_limit() -> None:
    builder, model, store = make_builder()
    registry = AgentRegistry(
        [
            make_agent("morning-walk", Lifecycle.FORMING, TargetShape.HABIT),
            make_agent("wind-down", Lifecycle.RESHAPED, TargetShape.STATE),
        ]
    )
    session, message = await builder.start("meditate daily", registry)
    assert session.stage is BuilderStage.ABANDONED
    assert "morning-walk" in message and "wind-down" in message
    assert "revisit" in message.lower()
    assert model.calls == []  # refused before any elicitation
    doc = await reload(store, session.id)
    assert doc["stage"] == "abandoned"


async def test_capacity_refusal_then_force_override() -> None:
    builder, _, store = make_builder(weekly_capacity=20, responses=list(HAPPY_SCRIPT))
    registry = AgentRegistry(
        [
            make_agent("training", Lifecycle.ESTABLISHED, TargetShape.SKILL, capacity=10),
            make_agent("study", Lifecycle.ESTABLISHED, TargetShape.PROJECT, capacity=8),
        ]
    )
    session, _ = await builder.start("read more", registry)
    session, _ = await builder.step(session.id, "I doomscroll at night", registry)
    # Design succeeds, but 18 active + 4 new > 20: honest refusal.
    session, message = await builder.step(session.id, "build it", registry)
    assert session.stage is BuilderStage.CAPACITY_CHECK
    assert "does not fit" in message
    assert "weekly budget of 20" in message
    doc = await reload(store, session.id)
    assert doc["stage"] == "capacity_check"

    # Any non-force message is treated as revision feedback. Here the model
    # re-emits the same oversized blueprint, so it still does not fit and we
    # stay at the capacity check rather than wedging.
    session, message = await builder.step(session.id, "hmm, what are my options?", registry)
    assert session.stage is BuilderStage.CAPACITY_CHECK

    # A conscious override proceeds to the proposal.
    session, message = await builder.step(session.id, "force", registry)
    assert session.stage is BuilderStage.AWAITING_APPROVAL
    assert "Proposal" in message


async def test_capacity_check_revises_to_fit() -> None:
    # The refusal invites the owner to "drop or shrink something". A shrink
    # request must actually revise the blueprint, not leave the owner stuck
    # between forcing past capacity and cancelling.
    small = blueprint_json(
        name="phone-curfew", description="One minute of wind-down."
    )
    # The shrunk design carries a cost of 1 (enforced floor for a habit), so
    # 18 active + 1 == 19 <= 20 and it fits.
    small = json.loads(small)
    small["manifest"]["capacity_cost"] = 1
    small = json.dumps(small)
    builder, _, store = make_builder(
        weekly_capacity=20, responses=[*HAPPY_SCRIPT, small]
    )
    registry = AgentRegistry(
        [
            make_agent("training", Lifecycle.ESTABLISHED, TargetShape.SKILL, capacity=10),
            make_agent("study", Lifecycle.ESTABLISHED, TargetShape.PROJECT, capacity=8),
        ]
    )
    session, _ = await builder.start("read more", registry)
    session, _ = await builder.step(session.id, "I doomscroll at night", registry)
    session, message = await builder.step(session.id, "build it", registry)
    assert session.stage is BuilderStage.CAPACITY_CHECK
    assert "does not fit" in message

    session, message = await builder.step(
        session.id, "make it much smaller then", registry
    )

    assert session.stage is BuilderStage.AWAITING_APPROVAL
    assert "Revised." in message
    assert "Proposal" in message
    assert session.blueprint is not None
    assert session.blueprint.manifest.capacity_cost == 1


# --- approval, revision, rejection ----------------------------------------------


async def drive_to_approval(
    builder: AgentBuilder, registry: AgentRegistry
) -> tuple[str, str]:
    session, _ = await builder.start("read more", registry)
    session, _ = await builder.step(session.id, "I doomscroll at night", registry)
    session, message = await builder.step(session.id, "build it", registry)
    assert session.stage is BuilderStage.AWAITING_APPROVAL
    return session.id, message


async def test_revision_loop_stays_awaiting_and_applies_revision() -> None:
    revised = blueprint_json(
        name="phone-curfew", description="Two pages of paper book after brushing teeth."
    )
    builder, model, store = make_builder(responses=[*HAPPY_SCRIPT, revised])
    registry = AgentRegistry()
    session_id, _ = await drive_to_approval(builder, registry)

    session, message = await builder.step(
        session_id, "smaller please: two pages, anchored to brushing my teeth", registry
    )
    assert session.stage is BuilderStage.AWAITING_APPROVAL
    assert session.blueprint is not None
    assert (
        session.blueprint.manifest.description
        == "Two pages of paper book after brushing teeth."
    )
    # Enforcement is reapplied to the revision too.
    assert session.blueprint.manifest.allowed_tools == []
    assert session.blueprint.manifest.lifecycle is Lifecycle.PROPOSED
    assert session.blueprint.manifest.schedule.kind == "daily"
    assert "Two pages" in message
    doc = await reload(store, session.id)
    assert doc["stage"] == "awaiting_approval"

    session, _ = await builder.step(session_id, "YES.", registry)
    assert session.stage is BuilderStage.DONE
    assert session.blueprint is not None
    assert session.blueprint.manifest.lifecycle is Lifecycle.FORMING


async def test_rejection_abandons_session() -> None:
    builder, _, store = make_builder(responses=list(HAPPY_SCRIPT))
    registry = AgentRegistry()
    session_id, _ = await drive_to_approval(builder, registry)

    session, message = await builder.step(session_id, "no", registry)
    assert session.stage is BuilderStage.ABANDONED
    assert session.blueprint is not None
    assert session.blueprint.manifest.lifecycle is Lifecycle.PROPOSED  # never approved
    doc = await reload(store, session_id)
    assert doc["stage"] == "abandoned"
    # No guilt in the goodbye.
    assert "no harm done" in message.lower()


async def test_cancel_keyword_abandons_mid_build() -> None:
    # A cancel word works at any stage (distinct from a bare "no", which is
    # an approval-stage rejection), so a session can never wedge the chat.
    builder, _, store = make_builder(responses=list(HAPPY_SCRIPT))
    registry = AgentRegistry()
    session, _ = await builder.start("read more", registry)
    session, _ = await builder.step(session.id, "I doomscroll at night", registry)
    assert session.stage is BuilderStage.DESIGNING  # mid-build, not at approval

    session, message = await builder.step(session.id, "never mind", registry)

    assert session.stage is BuilderStage.ABANDONED
    assert "no harm done" in message.lower()
    doc = await reload(store, session.id)
    assert doc["stage"] == "abandoned"
    # The abandoned session no longer intercepts later messages.
    assert await builder.active_session() is None


# --- session lookups -------------------------------------------------------------


async def test_get_session_and_active_session() -> None:
    model = FakeModel([ELICIT_Q1])
    clock = FakeClock()
    builder = AgentBuilder(model, StubUserModel(), MemoryStore(), clock)
    registry = AgentRegistry()

    assert await builder.get_session("nope") is None
    assert await builder.active_session() is None

    first, _ = await builder.start("read more", registry)
    clock.advance(minutes=5)
    model.push(ELICIT_Q1)
    second, _ = await builder.start("sleep earlier", registry)

    found = await builder.get_session(first.id)
    assert found is not None and found.stated_goal == "read more"

    active = await builder.active_session()
    assert active is not None and active.id == second.id  # most recent open one


async def test_active_session_skips_done_and_abandoned() -> None:
    builder, _, _ = make_builder(responses=list(HAPPY_SCRIPT))
    registry = AgentRegistry()
    session_id, _ = await drive_to_approval(builder, registry)
    await builder.step(session_id, "approve", registry)  # DONE
    assert await builder.active_session() is None
