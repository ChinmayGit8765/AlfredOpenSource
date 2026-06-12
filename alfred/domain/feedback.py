"""Closing the loop: turning outcomes into planning pressure.

Pure functions only. parse_outcome_report maps owner phrasing to an
OutcomeStatus deterministically (no model call for a one-word answer);
adherence_signal compresses AdherenceStats into one word; and
plan_adjustment_hint converts that word into an instruction for the next
planning prompt. A plan the owner repeatedly ignores is evidence the plan
is wrong, never that the owner is.
"""

from __future__ import annotations

import re

from alfred.domain.schemas import AdherenceStats, OutcomeStatus

_KEYWORDS: dict[OutcomeStatus, tuple[str, ...]] = {
    OutcomeStatus.DONE: ("done", "did it", "completed", "finished", "nailed it", "yes"),
    OutcomeStatus.PARTIAL: ("partial", "partially", "half", "some of it", "mostly"),
    OutcomeStatus.MISSED: (
        "missed",
        "didn't",
        "did not",
        "failed",
        "no",
        "not",
        "never",
        "haven't",
        "couldn't",
        "won't",
    ),
    OutcomeStatus.SKIPPED: ("skip", "skipped", "rest day", "pass"),
}

_PATTERNS: dict[OutcomeStatus, re.Pattern[str]] = {
    status: re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b")
    for status, words in _KEYWORDS.items()
}


def parse_outcome_report(text: str) -> OutcomeStatus | None:
    """Map owner phrasing to an OutcomeStatus; None when ambiguous or unmatched.

    Keywords match case-insensitively on word boundaries. When keywords
    from more than one status appear ("did half, then skipped") the report
    is ambiguous and the caller must ask, not guess. Negators ("not",
    "never", "haven't") count as MISSED keywords, so a negated completion
    like "not done" reads as ambiguous rather than DONE.
    """
    lowered = text.lower().replace("’", "'")
    matched = [status for status, pattern in _PATTERNS.items() if pattern.search(lowered)]
    if len(matched) == 1:
        return matched[0]
    return None


def adherence_signal(stats: AdherenceStats) -> str:
    """One word for follow-through: strong, ok, wobbling, or lapsing.

    Binding rule: one miss after successes still reads "strong" or "ok" by
    rate. One miss is fine; the second consecutive miss is the signal.
    No data is not a problem, so an empty record reads "ok".
    """
    if stats.consecutive_misses >= 2:
        return "lapsing"
    if stats.total == 0:
        return "ok"
    if stats.rate >= 0.8:
        return "strong"
    if stats.rate >= 0.5:
        return "ok"
    return "wobbling"


def plan_adjustment_hint(stats: AdherenceStats) -> str:
    """Instruction injected into the next planning prompt; empty when on track."""
    signal = adherence_signal(stats)
    if signal == "wobbling":
        return (
            "Recent follow-through is below half. Shrink the next plan: "
            "fewer items, lower load per item, and keep each commitment "
            "small enough to finish on a low-energy day."
        )
    if signal == "lapsing":
        return (
            "The previous plan was repeatedly missed. The plan is wrong, "
            "not the owner. Make the next plan drastically smaller and "
            "re-anchor each item to an existing cue that already happens "
            "every day."
        )
    return ""
