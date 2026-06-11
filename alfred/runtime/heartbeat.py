"""The heartbeat: what makes ALFRED proactive instead of reactive.

Each tick computes the due jobs (manifest schedules, lifecycle check-ins,
periodic reflection) and fires them through the injected runner. Last-run
state lives in the store, never in memory, so a restart cannot double-fire
a job that already ran.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from alfred.config import HeartbeatConfig
from alfred.domain.lifecycle import check_in_interval
from alfred.domain.registry import AgentRegistry
from alfred.domain.schemas import Collections, Schedule, ScheduledTrigger
from alfred.ports.clock import ClockPort
from alfred.ports.store import StorePort

logger = logging.getLogger(__name__)

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

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


def _threshold_today(now: datetime, hhmm: str | None) -> datetime | None:
    if not hhmm:
        return None
    try:
        at = time.fromisoformat(hhmm)
    except ValueError:
        return None
    return now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


def _schedule_due(schedule: Schedule, now: datetime, last: datetime | None) -> bool:
    if schedule.kind in ("daily", "weekly"):
        threshold = _threshold_today(now, schedule.time)
        if threshold is None or now < threshold:
            return False
        if schedule.kind == "weekly":
            wanted = {d.strip().lower()[:3] for d in schedule.days}
            if _DAY_NAMES[now.weekday()] not in wanted:
                return False
        return last is None or last < threshold
    if schedule.kind == "interval":
        if not schedule.every_minutes or schedule.every_minutes <= 0:
            return False
        return last is None or now - last >= timedelta(minutes=schedule.every_minutes)
    return False


@dataclass(frozen=True)
class _Job:
    """One potential firing: identity plus a pure due-ness predicate."""

    job_id: str
    agent: str
    reason: str
    due: _DuePredicate


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

    def _jobs(self) -> list[_Job]:
        # Rebuilt every tick so registry changes take effect immediately.
        jobs: list[_Job] = []
        for agent in self._registry.active():
            manifest = agent.manifest
            if manifest.schedule.kind != "none":
                jobs.append(
                    _Job(
                        job_id=f"schedule:{manifest.name}",
                        agent=manifest.name,
                        reason="schedule",
                        due=lambda now, last, s=manifest.schedule: _schedule_due(
                            s, now, last
                        ),
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
                    )
                )
        reflection_every = timedelta(days=self._config.reflection_days)
        jobs.append(
            _Job(
                job_id="reflection",
                agent="",
                reason="reflection",
                due=lambda now, last: last is None or now - last >= reflection_every,
            )
        )
        return jobs

    async def _last_run(self, job_id: str) -> datetime | None:
        doc = await self._store.get(Collections.HEARTBEAT, job_id)
        if not doc:
            return None
        raw = doc.get("last")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    async def tick(self) -> list[ScheduledTrigger]:
        """Fire every due job sequentially; return the fired triggers."""
        now = self._clock.now()
        if in_quiet_hours(now, self._config.quiet_hours):
            return []
        fired: list[ScheduledTrigger] = []
        for job in self._jobs():
            last = await self._last_run(job.job_id)
            if not job.due(now, last):
                continue
            trigger = ScheduledTrigger(agent=job.agent, reason=job.reason, at=now)
            try:
                await self._runner(trigger)
            except Exception:
                # A crashing job must not hot-loop: last-run still advances.
                logger.exception("scheduled job %s failed", job.job_id)
            await self._store.put(
                Collections.HEARTBEAT, job.job_id, {"last": now.isoformat()}
            )
            fired.append(trigger)
        return fired

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                # Store hiccups must not kill the pulse.
                logger.exception("heartbeat tick failed")
            await self._clock.sleep(self._config.tick_seconds)
