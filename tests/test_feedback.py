"""Tests for the pure feedback functions."""

from __future__ import annotations

import pytest

from alfred.domain.feedback import (
    adherence_signal,
    parse_outcome_report,
    plan_adjustment_hint,
)
from alfred.domain.schemas import AdherenceStats, OutcomeStatus

# ---------------------------------------------------------------------------
# parse_outcome_report
# ---------------------------------------------------------------------------

PARSE_CASES: list[tuple[str, OutcomeStatus | None]] = [
    # DONE keywords, with case variants
    ("done", OutcomeStatus.DONE),
    ("DONE", OutcomeStatus.DONE),
    ("Did it before breakfast", OutcomeStatus.DONE),
    ("completed", OutcomeStatus.DONE),
    ("finished the long run", OutcomeStatus.DONE),
    ("nailed it", OutcomeStatus.DONE),
    ("yes", OutcomeStatus.DONE),
    # PARTIAL
    ("partial credit today", OutcomeStatus.PARTIAL),
    ("Partially", OutcomeStatus.PARTIAL),
    ("got through half", OutcomeStatus.PARTIAL),
    ("some of it", OutcomeStatus.PARTIAL),
    ("mostly", OutcomeStatus.PARTIAL),
    # MISSED
    ("missed it", OutcomeStatus.MISSED),
    ("MiSsEd the session", OutcomeStatus.MISSED),
    ("didn't", OutcomeStatus.MISSED),
    ("didn’t make the gym", OutcomeStatus.MISSED),
    ("did not get around to it", OutcomeStatus.MISSED),
    ("failed", OutcomeStatus.MISSED),
    ("no", OutcomeStatus.MISSED),
    # SKIPPED
    ("skip", OutcomeStatus.SKIPPED),
    ("Skipped", OutcomeStatus.SKIPPED),
    ("rest day", OutcomeStatus.SKIPPED),
    ("pass", OutcomeStatus.SKIPPED),
    # ambiguous: keywords from more than one status
    ("did half, then skipped", None),
    ("mostly done", None),
    ("I did it but missed the stretching", None),
    # no keyword at all
    ("", None),
    ("went to the gym and lifted", None),
    # word boundaries: substrings of keywords must not match
    ("nothing to report", None),  # "no" inside "nothing"
    ("passive recovery planning", None),  # "pass" inside "passive"
    ("yesterday was busy", None),  # "yes" inside "yesterday"
]


@pytest.mark.parametrize(("text", "expected"), PARSE_CASES)
def test_parse_outcome_report(text: str, expected: OutcomeStatus | None) -> None:
    assert parse_outcome_report(text) == expected


# ---------------------------------------------------------------------------
# adherence_signal
# ---------------------------------------------------------------------------


def test_signal_no_data_is_ok() -> None:
    assert adherence_signal(AdherenceStats()) == "ok"


def test_signal_rate_exactly_point_eight_is_strong() -> None:
    stats = AdherenceStats(done=4, missed=1, consecutive_misses=1)
    assert stats.rate == 0.8
    assert adherence_signal(stats) == "strong"


def test_signal_rate_exactly_half_is_ok() -> None:
    stats = AdherenceStats(done=1, missed=1, consecutive_misses=1)
    assert stats.rate == 0.5
    assert adherence_signal(stats) == "ok"


def test_signal_below_half_is_wobbling() -> None:
    stats = AdherenceStats(done=1, missed=2, consecutive_misses=1)
    assert adherence_signal(stats) == "wobbling"


def test_signal_two_consecutive_misses_is_lapsing_even_at_high_rate() -> None:
    stats = AdherenceStats(done=8, missed=2, consecutive_misses=2)
    assert stats.rate == 0.8
    assert adherence_signal(stats) == "lapsing"


def test_signal_one_miss_after_successes_is_fine() -> None:
    # The binding rule: one miss never reads as a problem; catch the second.
    assert adherence_signal(AdherenceStats(done=4, missed=1, consecutive_misses=1)) == "strong"
    assert adherence_signal(AdherenceStats(done=2, missed=1, consecutive_misses=1)) == "ok"


def test_signal_partial_counts_half_in_rate() -> None:
    stats = AdherenceStats(partial=2)
    assert stats.rate == 0.5
    assert adherence_signal(stats) == "ok"


# ---------------------------------------------------------------------------
# plan_adjustment_hint
# ---------------------------------------------------------------------------


def test_hint_empty_for_strong_and_ok() -> None:
    assert plan_adjustment_hint(AdherenceStats(done=5)) == ""
    assert plan_adjustment_hint(AdherenceStats()) == ""
    assert plan_adjustment_hint(AdherenceStats(done=1, missed=1, consecutive_misses=1)) == ""


def test_hint_for_wobbling_shrinks_and_lowers_load() -> None:
    hint = plan_adjustment_hint(AdherenceStats(done=1, missed=3, consecutive_misses=1))
    low = hint.lower()
    assert "shrink" in low
    assert "lower" in low
    assert "load" in low


def test_hint_for_lapsing_blames_plan_not_owner() -> None:
    hint = plan_adjustment_hint(AdherenceStats(missed=3, consecutive_misses=3))
    low = hint.lower()
    assert "plan is wrong" in low
    assert "not the owner" in low
    assert "drastically smaller" in low
    assert "cue" in low


def test_hints_never_use_shame_vocabulary() -> None:
    for stats in (
        AdherenceStats(done=1, missed=3, consecutive_misses=1),
        AdherenceStats(missed=4, consecutive_misses=4),
    ):
        low = plan_adjustment_hint(stats).lower()
        for word in ("failure", "lazy", "disappointing"):
            assert word not in low
