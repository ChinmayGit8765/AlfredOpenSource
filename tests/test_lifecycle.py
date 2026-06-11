"""Tests for agent lifecycle rules: cadence, transitions, lapse diagnosis."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from alfred.domain.lifecycle import LapseDoctor, check_in_interval, next_lifecycle
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import (
    AdherenceStats,
    AgentManifest,
    LapseDiagnosis,
    Lifecycle,
    Outcome,
    OutcomeStatus,
    TargetShape,
)
from alfred.testing import FakeClock, FakeModel


def stats(
    done: int = 0,
    partial: int = 0,
    missed: int = 0,
    skipped: int = 0,
    consecutive: int = 0,
) -> AdherenceStats:
    return AdherenceStats(
        done=done,
        partial=partial,
        missed=missed,
        skipped=skipped,
        consecutive_misses=consecutive,
    )


def make_agent(
    name: str = "reading",
    shape: TargetShape | None = TargetShape.HABIT,
    lifecycle: Lifecycle = Lifecycle.LAPSING,
) -> LoadedAgent:
    return LoadedAgent(
        manifest=AgentManifest(
            name=name,
            description="Ten minutes of reading before bed.",
            shape=shape,
            lifecycle=lifecycle,
        ),
        prompt="You are the reading agent.",
    )


# --- check_in_interval ------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (Lifecycle.FORMING, timedelta(days=1)),
        (Lifecycle.LAPSING, timedelta(days=1)),
        (Lifecycle.RESHAPED, timedelta(days=1)),
        (Lifecycle.ESTABLISHED, timedelta(days=3)),
        (Lifecycle.MAINTENANCE, timedelta(days=7)),
        (Lifecycle.PROPOSED, None),
        (Lifecycle.PAUSED, None),
        (Lifecycle.RETIRED, None),
    ],
)
def test_check_in_interval_table(state: Lifecycle, expected: timedelta | None) -> None:
    assert check_in_interval(state) == expected


# --- next_lifecycle ---------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        Lifecycle.FORMING,
        Lifecycle.ESTABLISHED,
        Lifecycle.MAINTENANCE,
        Lifecycle.RESHAPED,
    ],
)
def test_two_consecutive_misses_means_lapsing(state: Lifecycle) -> None:
    assert next_lifecycle(state, stats(done=5, missed=2, consecutive=2)) is Lifecycle.LAPSING
    assert next_lifecycle(state, stats(done=5, missed=3, consecutive=3)) is Lifecycle.LAPSING


def test_lapse_detection_wins_over_promotion() -> None:
    # Rate and count qualify for ESTABLISHED, but the recent misses win.
    s = stats(done=12, missed=2, consecutive=2)
    assert s.total == 14 and s.rate >= 0.8
    assert next_lifecycle(Lifecycle.FORMING, s) is Lifecycle.LAPSING


@pytest.mark.parametrize("state", [Lifecycle.FORMING, Lifecycle.RESHAPED])
def test_promotion_to_established(state: Lifecycle) -> None:
    assert next_lifecycle(state, stats(done=14)) is Lifecycle.ESTABLISHED
    # Exact rate boundary: 12/15 == 0.8 qualifies.
    assert next_lifecycle(state, stats(done=12, missed=3)) is Lifecycle.ESTABLISHED


@pytest.mark.parametrize("state", [Lifecycle.FORMING, Lifecycle.RESHAPED])
def test_no_promotion_below_boundaries(state: Lifecycle) -> None:
    # One outcome short of the count threshold.
    assert next_lifecycle(state, stats(done=13)) is state
    # Count is there but rate 11/14 is below 0.8.
    assert next_lifecycle(state, stats(done=11, missed=3, consecutive=1)) is state


def test_established_to_maintenance() -> None:
    assert (
        next_lifecycle(Lifecycle.ESTABLISHED, stats(done=26, partial=2, missed=2))
        is Lifecycle.MAINTENANCE
    )
    # Exact rate boundary: (25 + 0.5) / 30 == 0.85 qualifies.
    assert (
        next_lifecycle(Lifecycle.ESTABLISHED, stats(done=25, partial=1, missed=4))
        is Lifecycle.MAINTENANCE
    )


def test_established_stays_below_maintenance_boundaries() -> None:
    # One outcome short of 30, perfect rate.
    assert next_lifecycle(Lifecycle.ESTABLISHED, stats(done=29)) is Lifecycle.ESTABLISHED
    # 25/30 is below 0.85.
    assert (
        next_lifecycle(Lifecycle.ESTABLISHED, stats(done=25, missed=5, consecutive=1))
        is Lifecycle.ESTABLISHED
    )


def test_lapsing_recovers_to_forming() -> None:
    # Misses cleared and rate at least 0.5: rebuild gently from FORMING.
    assert next_lifecycle(Lifecycle.LAPSING, stats(done=3, missed=3)) is Lifecycle.FORMING
    assert next_lifecycle(Lifecycle.LAPSING, stats(done=6, missed=2)) is Lifecycle.FORMING


def test_lapsing_stays_put_when_unsure() -> None:
    # Still missing: not recovered.
    assert (
        next_lifecycle(Lifecycle.LAPSING, stats(done=3, missed=3, consecutive=1))
        is Lifecycle.LAPSING
    )
    # No current misses but the rate is still under 0.5.
    assert next_lifecycle(Lifecycle.LAPSING, stats(done=2, missed=3)) is Lifecycle.LAPSING


@pytest.mark.parametrize(
    "state", [Lifecycle.PROPOSED, Lifecycle.PAUSED, Lifecycle.RETIRED]
)
def test_inert_states_never_auto_transition(state: Lifecycle) -> None:
    assert next_lifecycle(state, stats(missed=10, consecutive=5)) is state
    assert next_lifecycle(state, stats(done=50)) is state
    assert next_lifecycle(state, stats()) is state


def test_stay_put_default() -> None:
    # Not enough signal in any direction: nothing moves.
    assert next_lifecycle(Lifecycle.FORMING, stats(done=5)) is Lifecycle.FORMING
    assert next_lifecycle(Lifecycle.ESTABLISHED, stats(done=20)) is Lifecycle.ESTABLISHED
    assert next_lifecycle(Lifecycle.MAINTENANCE, stats(done=50)) is Lifecycle.MAINTENANCE
    assert (
        next_lifecycle(Lifecycle.RESHAPED, stats(done=3, missed=1, consecutive=1))
        is Lifecycle.RESHAPED
    )


# --- LapseDoctor ------------------------------------------------------------

SHRINK_JSON = json.dumps(
    {
        "cause": "too_big",
        "action": "shrink",
        "detail": "Ten minutes is too big after late shifts.",
        "new_size": "read two pages",
        "new_anchor": None,
    }
)

RETIRE_JSON = json.dumps(
    {
        "cause": "wrong_goal",
        "action": "retire",
        "detail": "The owner no longer wants this; letting it go is the win.",
        "new_size": None,
        "new_anchor": None,
    }
)


async def test_lapse_doctor_returns_validated_diagnosis() -> None:
    model = FakeModel([SHRINK_JSON])
    doctor = LapseDoctor(model, FakeClock())
    outcomes = [
        Outcome(agent="reading", status=OutcomeStatus.MISSED, report="fell asleep early"),
        Outcome(agent="reading", status=OutcomeStatus.MISSED, report=""),
        Outcome(agent="reading", status=OutcomeStatus.DONE, report="managed a chapter"),
    ]
    diagnosis = await doctor.diagnose(
        make_agent(),
        stats(done=4, missed=3, consecutive=2),
        outcomes,
        owner_comment="evenings got chaotic since the new job",
    )
    assert isinstance(diagnosis, LapseDiagnosis)
    assert diagnosis.cause == "too_big"
    assert diagnosis.action == "shrink"
    assert diagnosis.new_size == "read two pages"

    messages = model.calls[0]["messages"]
    system = messages[0].content
    user = next(m.content for m in messages if m.role == "user")

    # The stance: a lapse is data, never a moral failure.
    assert "data" in system.lower()
    assert "never a moral failure" in system.lower()
    assert "one miss is fine" in system.lower()
    assert "retirement" in system.lower()
    # No shame vocabulary anywhere in the system prompt.
    for word in ("lazy", "weak", "pathetic", "shame"):
        assert word not in system.lower()

    # The doctor sees the manifest, the numbers, the reports, the comment.
    assert "reading" in user
    assert "Ten minutes of reading before bed." in user
    assert "habit" in user
    assert "consecutive_misses=2" in user
    assert "missed=3" in user
    assert "fell asleep early" in user
    assert "evenings got chaotic since the new job" in user


async def test_lapse_doctor_retirement_is_reachable() -> None:
    model = FakeModel([RETIRE_JSON])
    doctor = LapseDoctor(model, FakeClock())
    diagnosis = await doctor.diagnose(
        make_agent(name="journaling"),
        stats(done=1, missed=6, consecutive=4),
        [],
        owner_comment="honestly I do not care about this anymore",
    )
    assert diagnosis.action == "retire"
    assert diagnosis.cause == "wrong_goal"
