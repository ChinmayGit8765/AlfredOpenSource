"""Tests for UserModelService against the real in-memory fakes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alfred.domain.schemas import Collections, Outcome, OutcomeStatus, UserProfile
from alfred.domain.user_model import UserModelService
from alfred.testing.fakes import FakeClock, MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(store: MemoryStore, clock: FakeClock) -> UserModelService:
    return UserModelService(store, clock)


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


async def test_get_profile_returns_default_when_missing(service: UserModelService) -> None:
    profile = await service.get_profile()
    assert profile == UserProfile()
    assert profile.version == 1
    assert profile.weekly_capacity == 20


async def test_save_profile_bumps_version_and_stamps_updated_at(
    service: UserModelService, clock: FakeClock
) -> None:
    profile = UserProfile(goals=["ship alfred"])
    await service.save_profile(profile)

    stored = await service.get_profile()
    assert stored.version == 2
    assert stored.updated_at == clock.now()
    assert stored.goals == ["ship alfred"]

    clock.advance(hours=2)
    await service.save_profile(stored)
    again = await service.get_profile()
    assert again.version == 3
    assert again.updated_at == clock.now()


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


async def test_record_observation_appends_in_order(
    service: UserModelService, store: MemoryStore, clock: FakeClock
) -> None:
    first = await service.record_observation("training", "preference", "prefers mornings")
    clock.advance(hours=1)
    second = await service.record_observation("owner", "constraint", "no time wednesdays")
    third = await service.record_observation("reflection", "insight", "momentum after day three")

    assert first.at is not None
    assert second.at == clock.now()

    recent = await service.recent_observations()
    assert [o.text for o in recent] == [third.text, second.text, first.text]

    limited = await service.recent_observations(limit=2)
    assert [o.text for o in limited] == [third.text, second.text]

    # appends accumulate; nothing is overwritten
    docs = await store.query(Collections.OBSERVATIONS)
    assert len(docs) == 3


async def test_record_observation_rejects_unknown_kind(service: UserModelService) -> None:
    with pytest.raises(ValidationError):
        await service.record_observation("owner", "vibe", "not a valid kind")


# ---------------------------------------------------------------------------
# outcomes and adherence
# ---------------------------------------------------------------------------


async def _record(service: UserModelService, status: OutcomeStatus, agent: str = "training") -> None:
    await service.record_outcome(Outcome(agent=agent, status=status))


async def test_record_outcome_counter_and_consecutive_miss_arithmetic(
    service: UserModelService,
) -> None:
    await _record(service, OutcomeStatus.MISSED)
    stats = (await service.get_profile()).adherence["training"]
    assert (stats.missed, stats.consecutive_misses) == (1, 1)

    # SKIPPED is neutral: increments its own counter, leaves the miss streak alone
    await _record(service, OutcomeStatus.SKIPPED)
    stats = (await service.get_profile()).adherence["training"]
    assert (stats.skipped, stats.consecutive_misses) == (1, 1)

    await _record(service, OutcomeStatus.MISSED)
    stats = (await service.get_profile()).adherence["training"]
    assert (stats.missed, stats.consecutive_misses) == (2, 2)

    await _record(service, OutcomeStatus.DONE)
    stats = (await service.get_profile()).adherence["training"]
    assert (stats.done, stats.consecutive_misses) == (1, 0)

    await _record(service, OutcomeStatus.MISSED)
    await _record(service, OutcomeStatus.PARTIAL)
    stats = (await service.get_profile()).adherence["training"]
    assert (stats.partial, stats.consecutive_misses) == (1, 0)
    assert stats.total == 6


async def test_skipped_never_starts_or_clears_a_miss_streak(
    service: UserModelService,
) -> None:
    await _record(service, OutcomeStatus.MISSED)
    await _record(service, OutcomeStatus.MISSED)
    await _record(service, OutcomeStatus.SKIPPED)
    stats = (await service.get_profile()).adherence["training"]
    assert stats.consecutive_misses == 2
    assert stats.skipped == 1


async def test_record_outcome_stamps_at_only_when_missing(
    service: UserModelService, clock: FakeClock
) -> None:
    await service.record_outcome(Outcome(agent="training", status=OutcomeStatus.DONE))
    explicit = datetime(2025, 12, 31, 8, 0, tzinfo=UTC)
    await service.record_outcome(
        Outcome(agent="training", status=OutcomeStatus.MISSED, at=explicit)
    )

    recent = await service.recent_outcomes()
    assert recent[0].at == explicit
    assert recent[1].at == clock.now()


async def test_adherence_updates_persist_across_get_profile_calls(
    service: UserModelService,
) -> None:
    await _record(service, OutcomeStatus.DONE)
    await _record(service, OutcomeStatus.DONE)

    first_read = await service.get_profile()
    second_read = await service.get_profile()
    assert first_read.adherence["training"].done == 2
    assert second_read.adherence["training"].done == 2
    # each record_outcome saved the profile, bumping the version each time
    assert second_read.version == 3


async def test_recent_outcomes_filters_by_agent_and_limits(
    service: UserModelService,
) -> None:
    await _record(service, OutcomeStatus.DONE, agent="training")
    await _record(service, OutcomeStatus.DONE, agent="study")
    await _record(service, OutcomeStatus.MISSED, agent="training")

    all_recent = await service.recent_outcomes()
    assert [(o.agent, o.status) for o in all_recent] == [
        ("training", OutcomeStatus.MISSED),
        ("study", OutcomeStatus.DONE),
        ("training", OutcomeStatus.DONE),
    ]

    training_only = await service.recent_outcomes(agent="training")
    assert [o.status for o in training_only] == [OutcomeStatus.MISSED, OutcomeStatus.DONE]

    limited = await service.recent_outcomes(limit=1)
    assert len(limited) == 1
    assert limited[0].status == OutcomeStatus.MISSED


# ---------------------------------------------------------------------------
# summary_for_prompt
# ---------------------------------------------------------------------------


async def test_summary_contains_profile_signal_and_observations(
    service: UserModelService,
) -> None:
    profile = await service.get_profile()
    profile.goals = ["run a marathon"]
    profile.constraints = ["no training on wednesdays"]
    profile.preferences = ["morning sessions"]
    await service.save_profile(profile)

    for _ in range(4):
        await _record(service, OutcomeStatus.DONE)
    await _record(service, OutcomeStatus.MISSED)
    await service.record_observation(
        "training", "adherence", "weights sessions land better in the morning"
    )

    summary = await service.summary_for_prompt()
    assert "run a marathon" in summary
    assert "no training on wednesdays" in summary
    assert "morning sessions" in summary
    assert "20" in summary  # weekly capacity
    assert "training: strong" in summary  # rate 0.8 with a single recent miss
    assert "rate 80%" in summary
    assert "weights sessions land better in the morning" in summary


async def test_summary_carries_no_shame_vocabulary(service: UserModelService) -> None:
    for _ in range(3):
        await _record(service, OutcomeStatus.MISSED)
    summary = (await service.summary_for_prompt()).lower()
    assert "training: lapsing" in summary
    for word in ("failure", "lazy", "disappointing"):
        assert word not in summary


async def test_summary_caps_observations_and_stays_compact(
    service: UserModelService,
) -> None:
    profile = await service.get_profile()
    profile.goals = [f"goal number {i}" for i in range(10)]
    await service.save_profile(profile)
    for i in range(20):
        await service.record_observation("owner", "event", f"observation number {i}")

    summary = await service.summary_for_prompt()
    observation_lines = [
        line for line in summary.splitlines() if line.startswith("- [event]")
    ]
    assert len(observation_lines) == 8
    assert "observation number 19" in summary  # newest first
    assert "observation number 0" not in summary
    assert len(summary.split()) <= 500
