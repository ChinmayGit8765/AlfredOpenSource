"""Periodic strategy review: the Conductor looks back so plans improve.

One reflection gathers the recent record (outcomes, observations, profile,
per-agent adherence) and asks the model for honest insights, durable
profile updates, and concrete proposals. Nothing here changes ALFRED
directly: profile updates append as notes, and every structural change,
including deterministic lifecycle transitions, becomes a pending Proposal
for the owner to rule on. Never silent edits.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from alfred.domain.feedback import adherence_signal
from alfred.domain.governance import Proposals
from alfred.domain.lifecycle import next_lifecycle
from alfred.domain.registry import AgentRegistry
from alfred.domain.schemas import (
    AdherenceStats,
    Collections,
    Observation,
    Outcome,
    Proposal,
    ProposalKind,
    Reflection,
    UserProfile,
)
from alfred.domain.structured import structured_call
from alfred.domain.user_model import UserModelService
from alfred.ports import ClockPort, ModelPort, StorePort

logger = logging.getLogger(__name__)

_OUTCOME_LIMIT = 100
_OBSERVATION_LIMIT = 40
_NOTES_CAP = 30

_SYSTEM_TEMPLATE = (
    "You are the Conductor, ALFRED's strategy layer, reviewing the last "
    "{days} days of the owner's record. insights are honest observations "
    "about what worked and what did not; name both plainly. profile_updates "
    "are durable facts about the owner worth keeping; not vibes, not one-off "
    "events. A plan the owner repeatedly ignored is a wrong plan, never a "
    "wrong owner. Make proposals only when you have something concrete to "
    "change: give kind, agent, summary, and reason. Respond with a single "
    "Reflection JSON object."
)


class ReflectionEngine:
    """Runs one periodic review and routes its effects through governance."""

    def __init__(
        self,
        model: ModelPort,
        user_model: UserModelService,
        store: StorePort,
        clock: ClockPort,
    ) -> None:
        self._model = model
        self._user_model = user_model
        self._store = store
        self._clock = clock
        self._proposals = Proposals(store, clock)

    async def reflect(self, registry: AgentRegistry, window_days: int = 7) -> Reflection:
        now = self._clock.now()
        profile = await self._user_model.get_profile()
        outcomes = await self._user_model.recent_outcomes(limit=_OUTCOME_LIMIT)
        cutoff = now - timedelta(days=window_days)
        outcomes = [o for o in outcomes if o.at is None or o.at >= cutoff]
        observations = await self._user_model.recent_observations(
            limit=_OBSERVATION_LIMIT
        )

        user = self._render_review(registry, profile, outcomes, observations, window_days)
        reflection = await structured_call(
            self._model,
            schema=Reflection,
            system=_SYSTEM_TEMPLATE.format(days=window_days),
            user=user,
        )

        await self._apply_profile_updates(reflection.profile_updates)

        created: list[Proposal] = []
        created.extend(await self._lifecycle_proposals(registry, profile))
        for proposed in reflection.proposals:
            # Re-create through governance so status is forced to pending
            # and created_at is stamped, whatever the model claimed.
            created.append(await self._proposals.create(proposed))

        final = reflection.model_copy(
            update={
                "window_days": window_days,
                "created_at": now,
                "proposals": created,
            }
        )
        await self._store.put(
            Collections.REFLECTIONS, final.id, final.model_dump(mode="json")
        )
        logger.info(
            "reflection complete: insights=%d profile_updates=%d proposals=%d",
            len(final.insights),
            len(final.profile_updates),
            len(final.proposals),
        )
        return final

    def _render_review(
        self,
        registry: AgentRegistry,
        profile: UserProfile,
        outcomes: list[Outcome],
        observations: list[Observation],
        window_days: int,
    ) -> str:
        lines = [
            f"Review window: the last {window_days} days "
            f"(today is {self._clock.now().date().isoformat()})."
        ]
        lines.append("Owner profile:")
        lines.append("- Goals: " + ("; ".join(profile.goals) or "none recorded"))
        lines.append(
            "- Constraints: " + ("; ".join(profile.constraints) or "none recorded")
        )
        lines.append(f"- Weekly capacity: {profile.weekly_capacity} points")

        lines.append("Active agents and adherence:")
        active = registry.active()
        if not active:
            lines.append("- none")
        for agent in active:
            name = agent.manifest.name
            stats = profile.adherence.get(name, AdherenceStats())
            lines.append(
                f"- {name} (lifecycle: {agent.manifest.lifecycle.value}): "
                f"signal={adherence_signal(stats)}, rate={stats.rate:.2f} over "
                f"{stats.total} outcomes, consecutive_misses="
                f"{stats.consecutive_misses}"
            )

        lines.append("Recent outcomes (newest first):")
        if not outcomes:
            lines.append("- none logged in this window")
        for outcome in outcomes:
            entry = f"- {outcome.agent}: {outcome.status.value}"
            if outcome.report:
                entry += f" ({outcome.report})"
            lines.append(entry)

        lines.append("Recent observations (newest first):")
        if not observations:
            lines.append("- none")
        for obs in observations:
            lines.append(f"- [{obs.kind}] ({obs.source}) {obs.text}")

        lines.append(
            "Review this record. What worked, what did not, what should change?"
        )
        return "\n".join(lines)

    async def _apply_profile_updates(self, updates: list[str]) -> None:
        if not updates:
            return
        # Re-read the profile inside the lock rather than writing back the
        # snapshot taken before the (slow) model call: a concurrent outcome
        # write that landed during the call must not be clobbered.
        async with self._user_model.transaction() as profile:
            profile.notes.extend(updates)
            # Keep the most recent notes only; the profile is a working
            # summary, the full history lives in the observations log.
            profile.notes = profile.notes[-_NOTES_CAP:]
        for update in updates:
            await self._user_model.record_observation(
                source="reflection", kind="insight", text=update
            )

    async def _lifecycle_proposals(
        self, registry: AgentRegistry, profile: UserProfile
    ) -> list[Proposal]:
        created: list[Proposal] = []
        for agent in registry.active():
            name = agent.manifest.name
            current = agent.manifest.lifecycle
            stats = profile.adherence.get(name, AdherenceStats())
            proposed = next_lifecycle(current, stats)
            if proposed == current:
                continue
            proposal = Proposal(
                kind=ProposalKind.LIFECYCLE_CHANGE,
                agent=name,
                summary=f"Move {name} from {current.value} to {proposed.value}",
                old=current.value,
                new=proposed.value,
                reason=(
                    f"rate {stats.rate:.2f} over {stats.total} logged outcomes, "
                    f"{stats.consecutive_misses} consecutive misses"
                ),
            )
            created.append(await self._proposals.create(proposal))
        return created
