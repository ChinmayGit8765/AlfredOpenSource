"""Tests for the pure domain models in schemas.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alfred.domain.feedback import adherence_signal
from alfred.domain.lifecycle import next_lifecycle
from alfred.domain.schemas import AdherenceStats, AgentManifest, Lifecycle

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


def test_total_counts_skips_but_engaged_does_not() -> None:
    # total counts every logged outcome (used for "has anything happened");
    # engaged excludes deliberate skips and is the maturity count lifecycle
    # promotion gates on, so skips cannot manufacture maturity.
    stats = AdherenceStats(done=4, partial=1, missed=2, skipped=3)
    assert stats.total == 10
    assert stats.engaged == 7


def test_skips_do_not_drag_signal_to_wobbling() -> None:
    # Six legitimate rest days alongside four done days must not inject
    # the "shrink the next plan" pressure into prompts.
    assert adherence_signal(AdherenceStats(done=4, skipped=6)) == "strong"


def test_forming_promotes_despite_deliberate_skips() -> None:
    # An owner who skips deliberately but never misses still earns
    # FORMING -> ESTABLISHED once enough real engagement has accrued.
    stats = AdherenceStats(done=14, skipped=4)
    assert stats.rate == 1.0 and stats.engaged == 14
    assert next_lifecycle(Lifecycle.FORMING, stats) is Lifecycle.ESTABLISHED


# ---------------------------------------------------------------------------
# AgentManifest.name: the only guard against a malicious agent name escaping
# the agents directory (it is joined straight into a filesystem path).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../evil",  # path traversal
        "a/b",  # path separator
        "/abs",  # absolute path
        "",  # empty
        "x",  # too short (min 2 chars)
        "Upper",  # uppercase
        "9lead",  # must start with a letter
        "has space",
        "a" * 60,  # too long (max 41)
    ],
)
def test_agent_manifest_name_rejects_unsafe_values(bad: str) -> None:
    with pytest.raises(ValidationError):
        AgentManifest(name=bad, description="d")


@pytest.mark.parametrize("good", ["training", "read-more_1", "phone-curfew"])
def test_agent_manifest_name_accepts_valid_slugs(good: str) -> None:
    assert AgentManifest(name=good, description="d").name == good
