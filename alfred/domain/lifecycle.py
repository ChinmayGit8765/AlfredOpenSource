"""Agent lifecycle rules: cadence, transitions, and lapse diagnosis.

Support scales inversely with automaticity: forming habits get daily
attention, established ones taper off. Transitions are deterministic and
conservative; the only LLM involvement is the LapseDoctor, whose stance
is binding: a lapse is data about whether the habit was the right one,
the right size, or the right time. It is never a moral failure.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import (
    AdherenceStats,
    LapseDiagnosis,
    Lifecycle,
    Outcome,
    Proposal,
    ProposalKind,
)
from alfred.domain.structured import structured_call
from alfred.ports import ClockPort, ModelPort

logger = logging.getLogger(__name__)

_INTERVALS: dict[Lifecycle, timedelta | None] = {
    Lifecycle.PROPOSED: None,
    Lifecycle.FORMING: timedelta(days=1),
    Lifecycle.LAPSING: timedelta(days=1),
    Lifecycle.RESHAPED: timedelta(days=1),
    Lifecycle.ESTABLISHED: timedelta(days=3),
    Lifecycle.MAINTENANCE: timedelta(days=7),
    Lifecycle.PAUSED: None,
    Lifecycle.RETIRED: None,
}

# States where the agent is actively being followed; only these can lapse.
_ACTIVE = frozenset(
    {
        Lifecycle.FORMING,
        Lifecycle.ESTABLISHED,
        Lifecycle.MAINTENANCE,
        Lifecycle.RESHAPED,
    }
)


def check_in_interval(state: Lifecycle) -> timedelta | None:
    """How often the heartbeat should check in for an agent in this state."""
    return _INTERVALS[state]


def next_lifecycle(state: Lifecycle, stats: AdherenceStats) -> Lifecycle:
    """Deterministic lifecycle transition table.

    Conservative by design: when unsure, stay put. PROPOSED, PAUSED, and
    RETIRED never auto-transition; only the owner moves those.
    """
    # Lapse detection wins over promotion: two consecutive misses is the
    # signal to diagnose, regardless of how good the long-run rate looks.
    if state in _ACTIVE and stats.consecutive_misses >= 2:
        return Lifecycle.LAPSING
    # Maturity gates count engaged outcomes, not total: deliberate skips are
    # not evidence a habit has formed, so they cannot manufacture promotion.
    if (
        state in (Lifecycle.FORMING, Lifecycle.RESHAPED)
        and stats.engaged >= 14
        and stats.rate >= 0.8
    ):
        return Lifecycle.ESTABLISHED
    if state is Lifecycle.ESTABLISHED and stats.engaged >= 30 and stats.rate >= 0.85:
        return Lifecycle.MAINTENANCE
    if state is Lifecycle.LAPSING and stats.consecutive_dones >= 3:
        # Recovered: three real completions in a row, per the binding
        # contract. Rebuild gently from FORMING rather than jumping back.
        return Lifecycle.FORMING
    return state


def lapse_proposal(
    agent_name: str, current: Lifecycle, diagnosis: LapseDiagnosis
) -> Proposal | None:
    """Translate a lapse diagnosis into one human-in-the-loop proposal.

    The spec's lapse response set (shrink, re-anchor, pause, reshape, retire)
    each becomes a pending Proposal the owner rules on; nothing here changes
    an agent silently. 'hold' returns None: the diagnosis found nothing worth
    changing yet (one miss is fine). Pause, reshape, and retire are lifecycle
    moves the runtime can apply on approval; shrink and re-anchor reshape the
    agent's design and are surfaced for the owner to apply by hand, carrying
    the doctor's specific guidance.
    """
    if diagnosis.action == "hold":
        return None
    reason = diagnosis.detail or f"diagnosed cause: {diagnosis.cause}"
    if diagnosis.action == "pause":
        return Proposal(
            kind=ProposalKind.LIFECYCLE_CHANGE,
            agent=agent_name,
            summary=f"Pause {agent_name} without guilt; revisit when it fits",
            old=current.value,
            new=Lifecycle.PAUSED.value,
            reason=reason,
        )
    if diagnosis.action == "reshape":
        return Proposal(
            kind=ProposalKind.LIFECYCLE_CHANGE,
            agent=agent_name,
            summary=f"Reshape {agent_name} into something that fits",
            old=current.value,
            new=Lifecycle.RESHAPED.value,
            reason=reason,
        )
    if diagnosis.action == "retire":
        return Proposal(
            kind=ProposalKind.RETIRE_AGENT,
            agent=agent_name,
            summary=f"Retire {agent_name} honestly; it no longer earns its place",
            old=current.value,
            new=Lifecycle.RETIRED.value,
            reason=reason,
        )
    if diagnosis.action == "shrink":
        target = diagnosis.new_size or "make the next step almost too small to fail"
        return Proposal(
            kind=ProposalKind.MANIFEST_CHANGE,
            agent=agent_name,
            summary=f"Shrink {agent_name}: {target}",
            reason=reason,
        )
    # reanchor
    target = diagnosis.new_anchor or "stack it onto a more reliable daily cue"
    return Proposal(
        kind=ProposalKind.PROMPT_CHANGE,
        agent=agent_name,
        summary=f"Re-anchor {agent_name}: {target}",
        reason=reason,
    )


_LAPSE_SYSTEM = (
    "You are ALFRED's lapse doctor. Your stance is binding: a lapse is data "
    "about whether the habit was the right one, the right size, or the right "
    "time. It is never a moral failure, and the owner is never the problem. "
    "One miss is fine and needs no response at all. A repeated lapse means "
    "one of: the habit was mis-sized (too big), mis-cued (a bad or missing "
    "anchor), life intervened (a real event took priority), or the goal is "
    "not truly wanted. Shrinking the habit, re-anchoring it to a better cue, "
    "pausing it without guilt, reshaping it into something that fits, and "
    "honest retirement are all first-class outcomes; recommending retirement "
    "when a goal no longer earns its place is a success, not a defeat. Never "
    "scold, never invoke streaks, never manufacture urgency. Recommend the "
    "smallest change that makes the behaviour likely again."
)


class LapseDoctor:
    """Diagnoses a lapsing agent and recommends the smallest true fix."""

    def __init__(self, model: ModelPort, clock: ClockPort) -> None:
        self._model = model
        self._clock = clock

    async def diagnose(
        self,
        agent: LoadedAgent,
        stats: AdherenceStats,
        recent_outcomes: list[Outcome],
        owner_comment: str = "",
    ) -> LapseDiagnosis:
        manifest = agent.manifest
        outcome_lines = [
            f"- {outcome.status.value}" + (f": {outcome.report}" if outcome.report else "")
            for outcome in recent_outcomes
        ] or ["- none logged"]
        user = "\n".join(
            [
                f"Agent: {manifest.name}",
                f"Description: {manifest.description}",
                f"Shape: {manifest.shape.value if manifest.shape else 'unspecified'}",
                f"Today: {self._clock.now().date().isoformat()}",
                (
                    f"Adherence: done={stats.done} partial={stats.partial} "
                    f"missed={stats.missed} skipped={stats.skipped} "
                    f"consecutive_misses={stats.consecutive_misses} "
                    f"rate={stats.rate:.2f} over {stats.total} logged outcomes"
                ),
                "Recent outcomes (newest first):",
                *outcome_lines,
                f"Owner's comment: {owner_comment or '(none)'}",
                "Diagnose this lapse and recommend exactly one action.",
            ]
        )
        diagnosis = await structured_call(
            self._model, schema=LapseDiagnosis, system=_LAPSE_SYSTEM, user=user
        )
        logger.info(
            "lapse diagnosis for %s: cause=%s action=%s",
            manifest.name,
            diagnosis.cause,
            diagnosis.action,
        )
        return diagnosis
