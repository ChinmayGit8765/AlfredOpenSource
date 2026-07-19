"""The evolving model of the owner.

The profile is a trend, not a snapshot: observations and outcomes append,
never overwrite, and the profile document is versioned on every save.
Everything here states facts about follow-through; judgment vocabulary is
banned because the summary lands inside agent prompts and tone leaks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from alfred.domain.feedback import adherence_signal
from alfred.domain.schemas import (
    AdherenceStats,
    Collections,
    Observation,
    Outcome,
    OutcomeStatus,
    UserProfile,
    load_or_none,
)
from alfred.ports import ClockPort, StorePort

logger = logging.getLogger(__name__)

_PROFILE_KEY = "current"
_SUMMARY_OBSERVATION_LIMIT = 8
_SUMMARY_WORD_BUDGET = 500


def _without_key(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != "_key"}


def _join_lines_within_budget(lines: list[str], budget: int) -> str:
    """Keep whole lines until the word budget runs out; never split a line."""
    kept: list[str] = []
    used = 0
    for line in lines:
        count = len(line.split())
        if kept and used + count > budget:
            break
        kept.append(line)
        used += count
    return "\n".join(kept)


class UserModelService:
    """Reads, appends to, and renders the persisted model of the owner."""

    def __init__(self, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock
        # Concurrent handlers (Discord tasks, heartbeat) share this service;
        # the lock keeps profile read-modify-write cycles from losing updates.
        self._profile_lock = asyncio.Lock()

    async def get_profile(self) -> UserProfile:
        doc = await self._store.get(Collections.PROFILE, _PROFILE_KEY)
        if doc is None:
            return UserProfile()
        profile = load_or_none(UserProfile, doc, source=Collections.PROFILE)
        if profile is None:
            # Never silently default over a drifted profile: transaction()
            # saves on exit, so a default here would let the next write
            # destroy the only copy. Quarantine the original first; the
            # fresh default that follows is then recoverable by hand.
            await self._store.put(
                Collections.PROFILE,
                f"unreadable-{self._clock.now().isoformat()}",
                _without_key(doc),
            )
            logger.error(
                "stored profile no longer validates; quarantined a copy and "
                "starting from a fresh default"
            )
            return UserProfile()
        return profile

    async def save_profile(self, profile: UserProfile) -> None:
        profile.version += 1
        profile.updated_at = self._clock.now()
        await self._store.put(
            Collections.PROFILE, _PROFILE_KEY, profile.model_dump(mode="json")
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[UserProfile]:
        """Atomic profile read-modify-write under the shared lock.

        The lock is held across a FRESH read and the save, so concurrent
        handlers (transport tasks and the heartbeat's reflection) cannot lose
        each other's updates. Every read-modify-save of the profile must go
        through here: holding the lock around only the save is not enough,
        because a snapshot taken before a slow model call is already stale by
        the time it is written back. Yields the current profile; saves on exit.
        """
        async with self._profile_lock:
            profile = await self.get_profile()
            yield profile
            await self.save_profile(profile)

    async def record_observation(self, source: str, kind: str, text: str) -> Observation:
        observation = Observation.model_validate(
            {"source": source, "kind": kind, "text": text, "at": self._clock.now()}
        )
        await self._store.append(
            Collections.OBSERVATIONS, observation.model_dump(mode="json")
        )
        return observation

    async def record_outcome(self, outcome: Outcome) -> None:
        if outcome.at is None:
            outcome.at = self._clock.now()
        await self._store.append(Collections.OUTCOMES, outcome.model_dump(mode="json"))

        async with self.transaction() as profile:
            stats = profile.adherence.setdefault(outcome.agent, AdherenceStats())
            if outcome.status is OutcomeStatus.DONE:
                stats.done += 1
                stats.consecutive_misses = 0
                stats.consecutive_dones += 1
            elif outcome.status is OutcomeStatus.PARTIAL:
                stats.partial += 1
                stats.consecutive_misses = 0
                # A partial clears the miss spiral but does not count toward
                # the 3-done streak a lapse recovery needs.
                stats.consecutive_dones = 0
            elif outcome.status is OutcomeStatus.MISSED:
                stats.missed += 1
                stats.consecutive_misses += 1
                stats.consecutive_dones = 0
            else:
                # SKIPPED is a deliberate choice, not a lapse: it neither
                # increments nor resets either streak.
                stats.skipped += 1
            # transaction() saves on exit.

    async def recent_observations(self, limit: int = 20) -> list[Observation]:
        docs = await self._store.query(
            Collections.OBSERVATIONS, limit=limit, newest_first=True
        )
        loaded = [
            load_or_none(Observation, doc, source=Collections.OBSERVATIONS)
            for doc in docs
        ]
        return [obs for obs in loaded if obs is not None]

    async def recent_outcomes(
        self, agent: str | None = None, limit: int = 20
    ) -> list[Outcome]:
        where = {"agent": agent} if agent is not None else None
        docs = await self._store.query(
            Collections.OUTCOMES, where=where, limit=limit, newest_first=True
        )
        loaded = [
            load_or_none(Outcome, doc, source=Collections.OUTCOMES) for doc in docs
        ]
        return [outcome for outcome in loaded if outcome is not None]

    async def summary_for_prompt(self) -> str:
        """Compact plain-text profile rendering for inclusion in agent prompts."""
        profile = await self.get_profile()
        observations = await self.recent_observations(limit=_SUMMARY_OBSERVATION_LIMIT)

        lines: list[str] = [f"Owner profile (v{profile.version})."]
        lines.append("Goals: " + ("; ".join(profile.goals) or "none recorded yet") + ".")
        lines.append(
            "Constraints: " + ("; ".join(profile.constraints) or "none recorded yet") + "."
        )
        lines.append(
            "Preferences: " + ("; ".join(profile.preferences) or "none recorded yet") + "."
        )
        lines.append(f"Weekly capacity: {profile.weekly_capacity} points.")

        if profile.adherence:
            lines.append("Adherence:")
            for name in sorted(profile.adherence):
                stats = profile.adherence[name]
                signal = adherence_signal(stats)
                if stats.engaged == 0:
                    # All-skips records land here too: deliberate skips
                    # carry no follow-through signal, and a skip must never
                    # render as a lapse.
                    note = (
                        f"{stats.skipped} deliberate skip(s), no engaged outcomes"
                        if stats.skipped
                        else "no outcomes logged yet"
                    )
                    lines.append(f"- {name}: {signal} ({note})")
                else:
                    lines.append(
                        f"- {name}: {signal} (rate {round(stats.rate * 100)}%, "
                        f"{stats.consecutive_misses} missed recently)"
                    )

        if observations:
            lines.append("Recent observations:")
            for obs in observations:
                lines.append(f"- [{obs.kind}] {obs.text[:200]}")

        return _join_lines_within_budget(lines, _SUMMARY_WORD_BUDGET)
