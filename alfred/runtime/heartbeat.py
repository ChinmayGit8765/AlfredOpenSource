"""The heartbeat: what makes ALFRED proactive instead of reactive.

Each tick computes the due jobs (manifest schedules, lifecycle check-ins,
periodic reflection) and fires them through the injected runner in
scheduled-time order. Last-run state lives in the store so a restart
cannot double-fire a job that already ran; memory only bridges a failed
store write within one process. A job whose runner raised keeps its
occurrence and retries with backoff instead of silently losing a whole
period to one transient failure.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from alfred.config import HeartbeatConfig
from alfred.domain.lifecycle import check_in_interval
from alfred.domain.registry import AgentRegistry
from alfred.domain.schemas import Collections, Schedule, ScheduledTrigger
from alfred.ports.clock import ClockPort
from alfred.ports.store import StorePort

logger = logging.getLogger(__name__)

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# A runner failure keeps the occurrence and retries with doubling backoff;
# at the cap the occurrence is abandoned (last advances) so a
# deterministically crashing job cannot burn model calls forever.
_FAILURE_CAP = 3
_MAX_BACKOFF = timedelta(hours=1)

# Retention sweeps run at most daily and delete in bounded batches so one
# sweep can never stall a tick on a huge backlog.
_SWEEP_EVERY = timedelta(days=1)
_SWEEP_BATCH = 500
_SWEPT_COLLECTIONS = (Collections.MESSAGES, Collections.AUDIT)

_DuePredicate = Callable[[datetime, "datetime | None"], bool]


def _parse_quiet(spec: str) -> tuple[time, time] | None:
    parts = spec.split("-")
    if len(parts) != 2:
        return None
    try:
        return time.fromisoformat(parts[0].strip()), time.fromisoformat(parts[1].strip())
    except ValueError:
        return None


def in_quiet_hours(now: datetime, spec: str) -> bool:
    """True when now falls inside the quiet window "HH:MM-HH:MM".

    The window may cross midnight. Start is inclusive, end exclusive, so
    the first tick at the end boundary fires normally. An empty, malformed,
    or zero-length spec means no quiet hours.
    """
    window = _parse_quiet(spec.strip()) if spec and spec.strip() else None
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    moment = now.time()
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def _parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def _most_recent_occurrence(schedule: Schedule, now: datetime) -> datetime | None:
    """Latest scheduled moment at or before now, looking back up to a week.

    Due-ness compares last-run against this occurrence rather than against
    today's threshold, so a firing deferred past midnight (quiet hours, or
    ALFRED simply not running) is caught up on the next tick instead of
    being lost until the schedule next comes around.
    """
    at = _parse_hhmm(schedule.time)
    if at is None:
        return None
    wanted = (
        {d.strip().lower()[:3] for d in schedule.days}
        if schedule.kind == "weekly"
        else None
    )
    for offset in range(8):
        day = now - timedelta(days=offset)
        candidate = day.replace(
            hour=at.hour, minute=at.minute, second=0, microsecond=0
        )
        if candidate > now:
            continue
        if wanted is not None and _DAY_NAMES[candidate.weekday()] not in wanted:
            continue
        return candidate
    return None


def _schedule_due(schedule: Schedule, now: datetime, last: datetime | None) -> bool:
    if schedule.kind in ("daily", "weekly"):
        occurrence = _most_recent_occurrence(schedule, now)
        if occurrence is None:
            return False
        if last is None:
            # A job with no history fires only for today's slot: catching up
            # on occurrences from before it was ever tracked would be noise.
            return occurrence.date() == now.date()
        return last < occurrence
    if schedule.kind == "interval":
        if not schedule.every_minutes or schedule.every_minutes <= 0:
            return False
        return last is None or now - last >= timedelta(minutes=schedule.every_minutes)
    return False


def _schedule_problem(schedule: Schedule) -> str | None:
    """A human-readable misconfiguration, or None when the schedule can fire."""
    if schedule.kind in ("daily", "weekly") and _parse_hhmm(schedule.time) is None:
        return f"{schedule.kind} schedule has a missing or invalid time {schedule.time!r}"
    if schedule.kind == "weekly":
        wanted = {d.strip().lower()[:3] for d in schedule.days}
        if not wanted & set(_DAY_NAMES):
            return f"weekly schedule has no valid days {schedule.days!r}"
    if schedule.kind == "interval" and (
        not schedule.every_minutes or schedule.every_minutes <= 0
    ):
        return f"interval schedule has invalid every_minutes {schedule.every_minutes!r}"
    return None


@dataclass(frozen=True)
class _Job:
    """One potential firing: identity plus a pure due-ness predicate.

    schedule/every exist so tick() can order same-tick firings by their
    intended time: on a catch-up tick (cold start, quiet hours ending) all
    of Monday morning becomes due at once, and registry-alphabetical order
    would run the 09:30 qa audit before the 08:00 planners have planned.
    """

    job_id: str
    agent: str
    reason: str
    due: _DuePredicate
    schedule: Schedule | None = None
    every: timedelta | None = None


@dataclass
class _JobState:
    """Persisted firing state for one job, tolerant of legacy doc shapes."""

    last: datetime | None
    failures: int
    failed_at: datetime | None


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class Heartbeat:
    """Fires due ScheduledTriggers through the injected runner."""

    def __init__(
        self,
        registry: AgentRegistry,
        clock: ClockPort,
        store: StorePort,
        runner: Callable[[ScheduledTrigger], Awaitable[None]],
        config: HeartbeatConfig,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._store = store
        self._runner = runner
        self._config = config
        spec = config.quiet_hours.strip()
        if spec and _parse_quiet(spec) is None:
            # Warn once here instead of every tick; suppression fails open.
            logger.warning("unparsable quiet_hours %r; quiet hours disabled", spec)
        # One warning per misconfigured schedule, not one per tick.
        self._warned: set[str] = set()
        # Bridges a failed last-run write within this process: the job DID
        # run, so a store hiccup must not re-fire it every tick until the
        # store recovers. The store stays the durable source across restarts.
        self._session_last: dict[str, datetime] = {}

    def _jobs(self) -> list[_Job]:
        # Rebuilt every tick so registry changes take effect immediately.
        jobs: list[_Job] = []
        for agent in self._registry.active():
            manifest = agent.manifest
            if manifest.schedule.kind != "none":
                problem = _schedule_problem(manifest.schedule)
                if problem is not None:
                    key = f"{manifest.name}:{problem}"
                    if key not in self._warned:
                        self._warned.add(key)
                        logger.warning(
                            "agent %s will never fire: %s", manifest.name, problem
                        )
                else:
                    jobs.append(
                        _Job(
                            job_id=f"schedule:{manifest.name}",
                            agent=manifest.name,
                            reason="schedule",
                            due=lambda now, last, s=manifest.schedule: _schedule_due(
                                s, now, last
                            ),
                            schedule=manifest.schedule,
                        )
                    )
            interval = check_in_interval(manifest.lifecycle)
            if interval is not None:
                jobs.append(
                    _Job(
                        job_id=f"check_in:{manifest.name}",
                        agent=manifest.name,
                        reason="check_in",
                        due=lambda now, last, iv=interval: last is None
                        or now - last >= iv,
                        every=interval,
                    )
                )
        reflection_every = timedelta(days=self._config.reflection_days)
        jobs.append(
            _Job(
                job_id="reflection",
                agent="",
                reason="reflection",
                due=lambda now, last: last is None or now - last >= reflection_every,
                every=reflection_every,
            )
        )
        if self._config.roadmap_nudge_days > 0:
            # A gentle surface of the owner's one next small win. Whether there
            # is anything to surface is the core's call (it holds the roadmap);
            # the heartbeat only owns the cadence.
            nudge_every = timedelta(days=self._config.roadmap_nudge_days)
            jobs.append(
                _Job(
                    job_id="roadmap_nudge",
                    agent="",
                    reason="roadmap_nudge",
                    due=lambda now, last: last is None or now - last >= nudge_every,
                    every=nudge_every,
                )
            )
        return jobs

    async def _job_state(self, job_id: str) -> _JobState:
        doc = await self._store.get(Collections.HEARTBEAT, job_id) or {}
        failures = doc.get("failures")
        return _JobState(
            last=_parse_iso(doc.get("last")),
            failures=failures if isinstance(failures, int) and failures > 0 else 0,
            failed_at=_parse_iso(doc.get("failed_at")),
        )

    def _effective_last(self, job_id: str, persisted: datetime | None) -> datetime | None:
        session = self._session_last.get(job_id)
        if persisted is None:
            return session
        if session is None:
            return persisted
        return max(persisted, session)

    def _backoff(self, failures: int) -> timedelta:
        # The first retry is never sooner than the next tick (no hot loop);
        # each further failure doubles the wait, capped so a transient
        # outage cannot push a daily job's retry out by hours.
        seconds = self._config.tick_seconds * (2 ** (failures - 1))
        return min(timedelta(seconds=seconds), _MAX_BACKOFF)

    @staticmethod
    def _fire_key(job: _Job, now: datetime, last: datetime | None) -> datetime:
        """When this due job was MEANT to fire; orders same-tick firings."""
        if job.schedule is not None:
            occurrence = _most_recent_occurrence(job.schedule, now)
            if occurrence is not None:
                return occurrence
        if job.every is not None and last is not None:
            return last + job.every
        # First-ever check-ins, reflection, and nudges key at now, so a
        # cold-start burst runs them after the caught-up schedules.
        return now

    async def tick(self) -> list[ScheduledTrigger]:
        """Fire every due job in scheduled-time order; return what ran."""
        now = self._clock.now()
        if in_quiet_hours(now, self._config.quiet_hours):
            return []

        due: list[tuple[datetime, int, _Job, _JobState]] = []
        for order, job in enumerate(self._jobs()):
            state = await self._job_state(job.job_id)
            last = self._effective_last(job.job_id, state.last)
            if not job.due(now, last):
                continue
            if (
                state.failures
                and state.failed_at is not None
                and now - state.failed_at < self._backoff(state.failures)
            ):
                continue
            due.append((self._fire_key(job, now, last), order, job, state))
        # Stable sort: equal keys keep registry order, so firing stays
        # deterministic.
        due.sort(key=lambda entry: (entry[0], entry[1]))

        fired: list[ScheduledTrigger] = []
        for _, _, job, state in due:
            trigger = ScheduledTrigger(agent=job.agent, reason=job.reason, at=now)
            try:
                await self._runner(trigger)
            except Exception:
                # The occurrence is kept and retried with backoff: one
                # transient model or transport failure at fire time must
                # not silently eat a whole period's planning run.
                logger.exception("scheduled job %s failed", job.job_id)
                await self._record_failure(job, state, now)
                continue
            self._session_last[job.job_id] = now
            await self._persist_last(job.job_id, now)
            fired.append(trigger)

        await self._maybe_sweep(now)
        return fired

    async def _persist_last(self, job_id: str, now: datetime) -> None:
        # Success replaces the whole doc, clearing any failure state.
        try:
            await self._store.put(
                Collections.HEARTBEAT, job_id, {"last": now.isoformat()}
            )
        except Exception:
            # The job DID run; _session_last already suppresses a re-fire
            # in this process, and later jobs in this tick must still fire.
            logger.exception(
                "failed to persist last-run for %s; suppressing re-fire in memory",
                job_id,
            )

    async def _record_failure(self, job: _Job, state: _JobState, now: datetime) -> None:
        failures = state.failures + 1
        if failures >= _FAILURE_CAP:
            # Abandon this occurrence: advancing last is what stops an
            # interval or check-in job from staying due forever with a
            # stale failure count attached.
            logger.error(
                "scheduled job %s abandoned for this period after %d attempts",
                job.job_id,
                failures,
            )
            self._session_last[job.job_id] = now
            await self._persist_last(job.job_id, now)
            return
        doc: dict[str, Any] = {"failed_at": now.isoformat(), "failures": failures}
        if state.last is not None:
            # Due-ness must stay anchored to the missed occurrence, so the
            # original last survives the failure record untouched.
            doc["last"] = state.last.isoformat()
        try:
            await self._store.put(Collections.HEARTBEAT, job.job_id, doc)
        except Exception:
            # Worst case the job retries on the next tick without backoff;
            # bounded by tick_seconds, and better than losing the record.
            logger.exception("failed to persist failure state for %s", job.job_id)

    async def _maybe_sweep(self, now: datetime) -> None:
        """Daily retention sweep over the append-only log collections.

        Off unless config.retention_days is positive. Only dated rows older
        than the cutoff are deleted, in bounded batches; undated or
        unparseable rows are never touched. Pending actions, proposals, and
        every other collection are deliberately out of scope: retention is
        for logs, never for anything an owner decision still hangs on.
        """
        if self._config.retention_days <= 0:
            return
        state = await self._job_state("retention")
        last = self._effective_last("retention", state.last)
        if last is not None and now - last < _SWEEP_EVERY:
            return
        cutoff = now - timedelta(days=self._config.retention_days)
        try:
            for collection in _SWEPT_COLLECTIONS:
                removed = 0
                # Oldest first (append keys are chronological), so the scan
                # can stop at the first row inside the retention window.
                docs = await self._store.query(collection, limit=_SWEEP_BATCH)
                for doc in docs:
                    at = _parse_iso(doc.get("at"))
                    if at is None:
                        continue
                    try:
                        expired = at < cutoff
                    except TypeError:
                        continue
                    if not expired:
                        break
                    if await self._store.delete(collection, doc["_key"]):
                        removed += 1
                if removed:
                    logger.info(
                        "retention sweep removed %d row(s) from %s", removed, collection
                    )
        finally:
            # A partial sweep is fine; it is idempotent and runs again
            # tomorrow. Advancing last keeps a failing sweep from hammering
            # the store every tick.
            self._session_last["retention"] = now
            await self._persist_last("retention", now)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                # Store hiccups must not kill the pulse.
                logger.exception("heartbeat tick failed")
            await self._clock.sleep(self._config.tick_seconds)
