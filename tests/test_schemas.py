"""Tests for the pure domain models in schemas.py."""

from __future__ import annotations

from alfred.domain.feedback import adherence_signal
from alfred.domain.lifecycle import next_lifecycle
from alfred.domain.schemas import AdherenceStats, Lifecycle

# ---------------------------------------------------------------------------
# AdherenceStats.rate: skipping is a deliberate choice, not a lapse
# ---------------------------------------------------------------------------


def test_rate_excludes_skips_from_denominator() -> None:
    # A deliberate skip must not read as a zero in the completion rate.
    assert AdherenceStats(done=4, skipped=6).rate == 1.0
    assert AdherenceStats(done=1, missed=1, skipped=8).rate == 0.5
    assert AdherenceStats(partial=2, skipped=3).rate == 0.5


def test_rate_zero_when_nothing_engaged() -> None:
    assert AdherenceStats().rate == 0.0
    assert AdherenceStats(skipped=5).rate == 0.0


def test_total_still_counts_skips_for_volume() -> None:
    # Volume thresholds (e.g. lifecycle's ">=14 logged outcomes") keep
    # counting skips: a skip is still a logged outcome.
    assert AdherenceStats(done=4, partial=1, missed=2, skipped=3).total == 10


def test_skips_do_not_drag_signal_to_wobbling() -> None:
    # Six legitimate rest days alongside four done days must not inject
    # the "shrink the next plan" pressure into prompts.
    assert adherence_signal(AdherenceStats(done=4, skipped=6)) == "strong"


def test_forming_promotes_despite_deliberate_skips() -> None:
    # An owner who skips >20% of days deliberately but never misses still
    # earns FORMING -> ESTABLISHED.
    stats = AdherenceStats(done=14, skipped=4)
    assert stats.rate == 1.0
    assert next_lifecycle(Lifecycle.FORMING, stats) is Lifecycle.ESTABLISHED
