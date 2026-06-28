"""Tests for roadmap-to-goal: small-win decomposition and the wins ledger."""

from __future__ import annotations

import json

from alfred.domain.roadmap import RoadmapPlanner, RoadmapService, WinsLedger
from alfred.domain.schemas import Collections, Milestone, Roadmap
from alfred.testing import FakeClock, FakeModel, MemoryStore

ROADMAP_JSON = json.dumps(
    {
        "goal": "ignored; the planner enforces the real goal",
        "milestones": [
            {
                "title": "Lay out clothes tonight",
                "why": "removes the morning friction that stalls a run",
                "done_signal": "clothes on the chair",
                "anchor": "after brushing teeth",
            },
            {
                "title": "Walk five minutes after coffee",
                "why": "builds the leaving-the-house habit",
                "done_signal": "back home, shoes off",
                "anchor": "after morning coffee",
            },
            {
                "title": "Walk fifteen minutes",
                "why": "extends the habit once leaving is automatic",
                "done_signal": "fifteen minutes on the clock",
                "anchor": "after morning coffee",
            },
        ],
    }
)


async def test_planner_builds_a_roadmap_of_small_wins() -> None:
    model = FakeModel([ROADMAP_JSON])
    planner = RoadmapPlanner(model, FakeClock())

    roadmap = await planner.plan("get fit", real_lever="move every morning")

    assert roadmap.goal == "get fit"  # enforced, not the model's stray value
    assert roadmap.real_lever == "move every morning"
    assert roadmap.created_at is not None
    # Exactly one active milestone (the first); the owner faces one next step.
    assert [m.status for m in roadmap.milestones] == ["active", "pending", "pending"]
    assert roadmap.next_win is not None
    assert roadmap.next_win.title == "Lay out clothes tonight"
    assert roadmap.won_count == 0

    # The planner is briefed with the small-wins stance.
    system = model.calls[0]["messages"][0].content.lower()
    assert "small wins" in system
    assert "almost too small to fail" in system
    assert "today" in system  # the first step must be doable now


def test_next_win_skips_won_milestones() -> None:
    roadmap = Roadmap(
        goal="g",
        milestones=[
            Milestone(title="a", status="won"),
            Milestone(title="b", status="pending"),
        ],
    )
    assert roadmap.next_win is not None and roadmap.next_win.title == "b"
    assert roadmap.won_count == 1


def test_next_win_is_none_when_every_milestone_is_won() -> None:
    roadmap = Roadmap(goal="g", milestones=[Milestone(title="a", status="won")])
    assert roadmap.next_win is None


async def test_planner_save_persists_the_roadmap() -> None:
    store = MemoryStore()
    planner = RoadmapPlanner(FakeModel([ROADMAP_JSON]), FakeClock())
    roadmap = await planner.plan("get fit")

    await planner.save(roadmap, store)

    doc = await store.get(Collections.ROADMAPS, roadmap.id)
    assert doc is not None and doc["goal"] == "get fit"


async def test_wins_ledger_records_and_lists_newest_first() -> None:
    store = MemoryStore()
    clock = FakeClock()
    ledger = WinsLedger(store, clock)

    await ledger.record("laid out clothes", source="owner", goal="get fit")
    clock.advance(minutes=1)
    await ledger.record("walked five minutes", source="milestone", goal="get fit")

    wins = await ledger.recent()
    assert [w.text for w in wins] == ["walked five minutes", "laid out clothes"]
    assert wins[0].source == "milestone"
    assert wins[0].at is not None
    assert all(w.goal == "get fit" for w in wins)


# ---------------------------------------------------------------------------
# RoadmapService: the one live path the runtime drives
# ---------------------------------------------------------------------------


def _service(
    store: MemoryStore, model: FakeModel, clock: FakeClock | None = None
) -> RoadmapService:
    clock = clock or FakeClock()
    planner = RoadmapPlanner(model, clock)
    wins = WinsLedger(store, clock)
    return RoadmapService(planner, wins, store, clock)


async def test_service_set_goal_persists_current_with_one_next_win() -> None:
    store = MemoryStore()
    service = _service(store, FakeModel([ROADMAP_JSON]))

    roadmap = await service.set_goal("get fit", real_lever="move every morning")

    assert roadmap.goal == "get fit"
    current = await service.current()  # read back from the store, not the return
    assert current is not None
    assert current.real_lever == "move every morning"
    assert current.next_win is not None
    assert current.next_win.title == "Lay out clothes tonight"


async def test_service_current_is_none_before_a_goal_is_set() -> None:
    service = _service(MemoryStore(), FakeModel())
    assert await service.current() is None


async def test_service_complete_next_advances_and_logs_a_win() -> None:
    store = MemoryStore()
    service = _service(store, FakeModel([ROADMAP_JSON]))
    await service.set_goal("get fit")

    roadmap, won, new_next = await service.complete_next()

    assert won is not None and won.title == "Lay out clothes tonight"
    assert won.status == "won"
    assert new_next is not None
    assert new_next.title == "Walk five minutes after coffee"
    assert new_next.status == "active"
    assert roadmap is not None and roadmap.won_count == 1

    # The win lands in the momentum ledger, sourced from the milestone.
    wins = await service.recent_wins()
    assert [w.text for w in wins] == ["Lay out clothes tonight"]
    assert wins[0].source == "milestone"
    assert wins[0].goal == "get fit"

    # Persisted: a fresh read sees the advanced state, one active step.
    reloaded = await service.current()
    assert reloaded is not None
    assert [m.status for m in reloaded.milestones] == ["won", "active", "pending"]


async def test_service_complete_next_with_no_goal_is_a_no_op() -> None:
    service = _service(MemoryStore(), FakeModel())
    roadmap, won, new_next = await service.complete_next()
    assert roadmap is None and won is None and new_next is None


async def test_service_completing_the_last_win_finishes_the_road() -> None:
    store = MemoryStore()
    service = _service(store, FakeModel([ROADMAP_JSON]))
    await service.set_goal("get fit")
    await service.complete_next()
    await service.complete_next()

    roadmap, won, new_next = await service.complete_next()  # the third and last

    assert won is not None and won.title == "Walk fifteen minutes"
    assert new_next is None  # nothing left; the road is complete
    assert roadmap is not None and roadmap.won_count == 3

    # A further attempt finds nothing to win, never an error.
    _, nothing, _ = await service.complete_next()
    assert nothing is None


async def test_service_set_goal_archives_the_previous_roadmap() -> None:
    store = MemoryStore()
    service = _service(store, FakeModel([ROADMAP_JSON]))
    first = await service.set_goal("get fit")
    second = await service.set_goal("learn guitar")

    # The new goal is current; the old one is archived by its id, not lost.
    current = await service.current()
    assert current is not None and current.id == second.id and current.goal == "learn guitar"
    archived = await store.get(Collections.ROADMAPS, first.id)
    assert archived is not None and archived["goal"] == "get fit"


async def test_service_record_win_logs_against_current_goal_without_advancing() -> None:
    store = MemoryStore()
    service = _service(store, FakeModel([ROADMAP_JSON]))
    await service.set_goal("get fit")

    win = await service.record_win("ran an unplanned 5k")

    assert win.goal == "get fit"
    assert win.source == "owner"
    # A standalone win is momentum, not a milestone: the road does not advance.
    current = await service.current()
    assert current is not None and current.won_count == 0
