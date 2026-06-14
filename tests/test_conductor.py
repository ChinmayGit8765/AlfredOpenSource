"""Tests for the Conductor: conflict detection and capacity-safe reconcile."""

from __future__ import annotations

from datetime import date

from alfred.domain.conductor import Conductor, detect_conflicts
from alfred.domain.schemas import (
    Adjustment,
    Plan,
    PlanItem,
    ReconciledSchedule,
    UserProfile,
)
from alfred.testing.fakes import FakeClock, FakeModel


def item(item_id: str, **kwargs: object) -> PlanItem:
    kwargs.setdefault("title", item_id)
    return PlanItem(id=item_id, **kwargs)  # type: ignore[arg-type]


def plan(agent: str, items: list[PlanItem], week_of: date | None = None) -> Plan:
    return Plan(id=f"plan-{agent}", agent=agent, week_of=week_of, items=items)


def overload_kinds(plans: list[Plan], capacity: int) -> list[str]:
    return [
        c.kind
        for c in detect_conflicts(plans, capacity)
        if c.kind in ("overload_week", "overload_day")
    ]


# ---------------------------------------------------------------------------
# detect_conflicts: overload_week
# ---------------------------------------------------------------------------


def test_week_load_equal_to_capacity_is_not_overload() -> None:
    plans = [
        plan("training", [item("a1", load=3)]),
        plan("study", [item("b1", load=3)]),
    ]
    assert detect_conflicts(plans, 6) == []


def test_week_load_one_over_capacity_is_overload() -> None:
    plans = [
        plan("training", [item("a1", load=3)]),
        plan("study", [item("b1", load=4)]),
    ]
    conflicts = detect_conflicts(plans, 6)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.kind == "overload_week"
    assert sorted(conflict.item_ids) == ["a1", "b1"]
    assert "training" in conflict.detail
    assert "study" in conflict.detail


# ---------------------------------------------------------------------------
# detect_conflicts: overload_day and day normalization
# ---------------------------------------------------------------------------


def test_day_load_equal_to_daily_capacity_is_not_overload() -> None:
    # capacity 20 -> daily cap ceil(20/5) = 4
    plans = [plan("training", [item("a1", day="mon", load=2), item("a2", day="mon", load=2)])]
    assert detect_conflicts(plans, 20) == []


def test_day_load_one_over_daily_capacity_is_overload() -> None:
    plans = [
        plan(
            "training",
            [
                item("a1", day="mon", load=2),
                item("a2", day="mon", load=2),
                item("a3", day="mon", load=1),
            ],
        )
    ]
    conflicts = detect_conflicts(plans, 20)
    assert [c.kind for c in conflicts] == ["overload_day"]
    assert conflicts[0].day == "mon"
    assert sorted(conflicts[0].item_ids) == ["a1", "a2", "a3"]
    assert "training" in conflicts[0].detail


def test_day_normalization_groups_spellings_and_iso_dates() -> None:
    # "Monday", "MON", and 2026-06-08 (a Monday) all land on "mon".
    plans = [
        plan("training", [item("a1", day="Monday", load=2)]),
        plan("study", [item("b1", day="MON", load=2)]),
        plan("build", [item("c1", day="2026-06-08", load=1)]),
    ]
    conflicts = detect_conflicts(plans, 20)
    assert [c.kind for c in conflicts] == ["overload_day"]
    assert conflicts[0].day == "mon"
    assert sorted(conflicts[0].item_ids) == ["a1", "b1", "c1"]


def test_items_without_day_count_toward_week_only() -> None:
    # Load 5 on no particular day: no daily conflict, and within the week.
    plans = [plan("training", [item("a1", load=5)])]
    assert detect_conflicts(plans, 20) == []


# ---------------------------------------------------------------------------
# detect_conflicts: time_collision
# ---------------------------------------------------------------------------


def test_back_to_back_items_do_not_collide() -> None:
    plans = [
        plan("training", [item("a1", day="mon", time="09:00", duration_min=60)]),
        plan("study", [item("b1", day="mon", time="10:00", duration_min=60)]),
    ]
    assert detect_conflicts(plans, 20) == []


def test_overlapping_items_collide() -> None:
    plans = [
        plan("training", [item("a1", day="mon", time="09:00", duration_min=60)]),
        plan("study", [item("b1", day="mon", time="09:30", duration_min=60)]),
    ]
    conflicts = detect_conflicts(plans, 20)
    assert [c.kind for c in conflicts] == ["time_collision"]
    assert conflicts[0].day == "mon"
    assert sorted(conflicts[0].item_ids) == ["a1", "b1"]
    assert "training" in conflicts[0].detail
    assert "study" in conflicts[0].detail


def test_missing_duration_defaults_to_60_minutes() -> None:
    plans = [
        plan("training", [item("a1", day="mon", time="09:00")]),  # no duration
        plan("study", [item("b1", day="mon", time="09:45", duration_min=30)]),
    ]
    conflicts = detect_conflicts(plans, 20)
    assert [c.kind for c in conflicts] == ["time_collision"]


def test_zero_duration_items_at_same_minute_collide() -> None:
    # An explicit duration_min=0 is a valid value (ge=0). Two such items at
    # the same minute must still register as a collision rather than slipping
    # through a zero-length window.
    plans = [
        plan("training", [item("a1", day="mon", time="09:00", duration_min=0)]),
        plan("study", [item("b1", day="mon", time="09:00", duration_min=0)]),
    ]
    conflicts = detect_conflicts(plans, 20)
    assert [c.kind for c in conflicts] == ["time_collision"]


def test_zero_duration_item_does_not_collide_at_a_neighbour_boundary() -> None:
    # A zero-duration marker at the exact end of another item is back-to-back,
    # not overlapping.
    plans = [
        plan("training", [item("a1", day="mon", time="09:00", duration_min=60)]),
        plan("study", [item("b1", day="mon", time="10:00", duration_min=0)]),
    ]
    assert detect_conflicts(plans, 20) == []


def test_collision_requires_both_items_timed() -> None:
    plans = [
        plan("training", [item("a1", day="mon", time="09:00", duration_min=60)]),
        plan("study", [item("b1", day="mon")]),  # no time
    ]
    assert detect_conflicts(plans, 20) == []


def test_unparseable_time_is_treated_as_no_time() -> None:
    plans = [
        plan("training", [item("a1", day="mon", time="09:00", duration_min=60)]),
        plan("study", [item("b1", day="mon", time="quarter past nine")]),
    ]
    assert detect_conflicts(plans, 20) == []


def test_collision_day_normalization_across_spellings() -> None:
    plans = [
        plan("training", [item("a1", day="Monday", time="09:00", duration_min=60)]),
        plan("study", [item("b1", day="2026-06-08", time="09:30", duration_min=30)]),
    ]
    conflicts = detect_conflicts(plans, 20)
    assert [c.kind for c in conflicts] == ["time_collision"]
    assert conflicts[0].day == "mon"


# ---------------------------------------------------------------------------
# Conductor.reconcile: passthrough paths
# ---------------------------------------------------------------------------


async def test_empty_input_passthrough_makes_zero_model_calls() -> None:
    model = FakeModel()
    conductor = Conductor(model, FakeClock())
    result = await conductor.reconcile([], UserProfile(weekly_capacity=20))
    assert model.calls == []
    assert result.plans == []
    assert result.total_load == 0
    assert result.week_of is None
    assert result.summary


async def test_clean_multi_plan_passthrough_makes_zero_model_calls() -> None:
    model = FakeModel()
    conductor = Conductor(model, FakeClock())
    plans = [
        plan("training", [item("a1", day="mon", load=2)], week_of=date(2026, 6, 8)),
        plan("study", [item("b1", day="tue", load=3)], week_of=date(2026, 6, 1)),
    ]
    result = await conductor.reconcile(plans, UserProfile(weekly_capacity=20))
    assert model.calls == []
    assert result.plans == plans
    assert result.total_load == 5
    assert result.week_of == date(2026, 6, 1)  # earliest week_of wins
    assert result.adjustments == []
    assert result.summary and "\n" not in result.summary


# ---------------------------------------------------------------------------
# Conductor.reconcile: conflict path with a valid model resolution
# ---------------------------------------------------------------------------


def conflicting_plans() -> list[Plan]:
    return [
        plan("training", [item("a1", load=3)], week_of=date(2026, 6, 8)),
        plan("study", [item("b1", load=3)], week_of=date(2026, 6, 8)),
    ]


async def test_conflict_path_uses_exactly_one_model_call() -> None:
    resolution = ReconciledSchedule(
        week_of=date(2026, 6, 8),
        plans=[
            plan("training", [item("a1", load=3)]),
            plan("study", []),
        ],
        adjustments=[
            Adjustment(agent="study", item_id="b1", action="drop", detail="over capacity")
        ],
        total_load=3,
        summary="dropped study item to fit",
    )
    model = FakeModel([resolution.model_dump_json()])
    conductor = Conductor(model, FakeClock())
    result = await conductor.reconcile(
        conflicting_plans(), UserProfile(weekly_capacity=4)
    )
    assert len(model.calls) == 1
    assert overload_kinds(result.plans, 4) == []
    assert result.total_load == 3
    assert {i.id for p in result.plans for i in p.items} == {"a1"}
    assert result.adjustments[0].action == "drop"
    assert result.week_of == date(2026, 6, 8)
    # the prompt carried the plans, the capacity, and the conflicts
    user_text = model.calls[0]["messages"][-1].content
    assert "Weekly capacity: 4" in user_text
    assert "overload_week" in user_text
    assert '"a1"' in user_text


# ---------------------------------------------------------------------------
# Conductor.reconcile: backstop and deterministic fallback
# ---------------------------------------------------------------------------


async def test_over_capacity_model_output_triggers_fallback() -> None:
    # Model "resolves" by changing nothing: still 6 points against capacity 4.
    unresolved = ReconciledSchedule(
        plans=conflicting_plans(), total_load=6, summary="all good"
    )
    model = FakeModel([unresolved.model_dump_json()])
    conductor = Conductor(model, FakeClock())
    result = await conductor.reconcile(
        conflicting_plans(), UserProfile(weekly_capacity=4)
    )
    assert len(model.calls) == 1
    assert overload_kinds(result.plans, 4) == []
    assert result.total_load <= 4
    assert result.warnings, "fallback must warn the owner"
    assert any("fallback" in w for w in result.warnings)
    assert result.adjustments
    assert all(a.action == "drop" for a in result.adjustments)


async def test_invented_item_id_never_appears_in_result() -> None:
    invented = ReconciledSchedule(
        plans=[
            plan("training", [item("a1", load=1)]),
            plan("study", [item("zzz-invented", load=1)]),
        ],
        total_load=2,
        summary="made something up",
    )
    model = FakeModel([invented.model_dump_json()])
    conductor = Conductor(model, FakeClock())
    inputs = conflicting_plans()
    known = {i.id for p in inputs for i in p.items}
    result = await conductor.reconcile(inputs, UserProfile(weekly_capacity=4))
    result_ids = {i.id for p in result.plans for i in p.items}
    assert "zzz-invented" not in result_ids
    assert result_ids <= known
    assert overload_kinds(result.plans, 4) == []


async def test_fallback_drops_lowest_load_from_most_overloaded_day() -> None:
    # capacity 10 -> daily cap 2; mon carries 5 points and the week 11.
    def fresh_plans() -> list[Plan]:
        return [
            plan(
                "training",
                [
                    item("t1", day="mon", load=2),
                    item("t2", day="mon", load=2),
                    item("t3", day="tue", load=1),
                ],
                week_of=date(2026, 6, 8),
            ),
            plan(
                "study",
                [item("s1", day="mon", load=1), item("s2", load=5)],
                week_of=date(2026, 6, 8),
            ),
        ]

    unresolved = ReconciledSchedule(plans=fresh_plans(), total_load=11)
    model = FakeModel([unresolved.model_dump_json()])
    conductor = Conductor(model, FakeClock())
    result = await conductor.reconcile(fresh_plans(), UserProfile(weekly_capacity=10))
    # mon (load 5) prunes s1 (lowest load) then t1 (tie with t2, alphabetical)
    assert [a.item_id for a in result.adjustments] == ["s1", "t1"]
    assert overload_kinds(result.plans, 10) == []
    assert result.total_load == 8


async def test_fallback_is_deterministic() -> None:
    def fresh_plans() -> list[Plan]:
        return [
            plan(
                "training",
                [
                    item("t1", day="mon", load=2),
                    item("t2", day="mon", load=2),
                    item("t3", day="tue", load=1),
                ],
                week_of=date(2026, 6, 8),
            ),
            plan(
                "study",
                [item("s1", day="mon", load=1), item("s2", load=5)],
                week_of=date(2026, 6, 8),
            ),
        ]

    unresolved = ReconciledSchedule(plans=fresh_plans(), total_load=11)

    async def run_once() -> ReconciledSchedule:
        model = FakeModel([unresolved.model_dump_json()])
        conductor = Conductor(model, FakeClock())
        return await conductor.reconcile(fresh_plans(), UserProfile(weekly_capacity=10))

    first = await run_once()
    second = await run_once()
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


async def test_unusable_model_output_still_returns_within_capacity() -> None:
    # The model never produces valid JSON; reconcile must not crash and
    # must still return a schedule that fits.
    model = FakeModel(["this is not json at all"])
    conductor = Conductor(model, FakeClock())
    result = await conductor.reconcile(
        conflicting_plans(), UserProfile(weekly_capacity=4)
    )
    assert overload_kinds(result.plans, 4) == []
    assert result.total_load <= 4
    assert result.warnings
