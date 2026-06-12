"""Heartbeat tests: cadences, quiet hours, restart safety, crash isolation."""

from __future__ import annotations

from datetime import datetime, timezone

from alfred.config import HeartbeatConfig
from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.schemas import (
    AgentManifest,
    Collections,
    Lifecycle,
    Schedule,
    ScheduledTrigger,
)
from alfred.runtime.heartbeat import Heartbeat, in_quiet_hours
from alfred.testing.fakes import FakeClock, MemoryStore

MONDAY = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)  # a Monday, 09:00


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[ScheduledTrigger] = []

    async def __call__(self, trigger: ScheduledTrigger) -> None:
        self.calls.append(trigger)


class FlakyRunner(RecordingRunner):
    """Raises for the agent named 'bad'; records everything else."""

    async def __call__(self, trigger: ScheduledTrigger) -> None:
        if trigger.agent == "bad":
            raise RuntimeError("boom")
        await super().__call__(trigger)


def agent(
    name: str,
    *,
    lifecycle: Lifecycle = Lifecycle.ESTABLISHED,
    **schedule_kwargs: object,
) -> LoadedAgent:
    manifest = AgentManifest(
        name=name,
        description=f"{name} test agent",
        lifecycle=lifecycle,
        schedule=Schedule(**schedule_kwargs) if schedule_kwargs else Schedule(),
    )
    return LoadedAgent(manifest=manifest, prompt="be useful")


def build(
    agents: list[LoadedAgent],
    *,
    clock: FakeClock,
    store: MemoryStore | None = None,
    runner: RecordingRunner | None = None,
    **config_kwargs: object,
) -> tuple[Heartbeat, MemoryStore, RecordingRunner]:
    config_kwargs.setdefault("quiet_hours", "")
    store = store or MemoryStore()
    runner = runner or RecordingRunner()
    heartbeat = Heartbeat(
        AgentRegistry(agents), clock, store, runner, HeartbeatConfig(**config_kwargs)
    )
    return heartbeat, store, runner


def schedule_calls(runner: RecordingRunner, name: str) -> list[ScheduledTrigger]:
    return [t for t in runner.calls if t.reason == "schedule" and t.agent == name]


def check_in_calls(runner: RecordingRunner, name: str) -> list[ScheduledTrigger]:
    return [t for t in runner.calls if t.reason == "check_in" and t.agent == name]


async def test_daily_fires_after_time_once_per_day_then_next_day() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, _, runner = build(
        [agent("training", kind="daily", time="10:00")], clock=clock
    )

    await heartbeat.tick()  # 09:00, before the daily time
    assert schedule_calls(runner, "training") == []

    clock.advance(hours=1, minutes=30)  # 10:30
    await heartbeat.tick()
    assert len(schedule_calls(runner, "training")) == 1

    clock.advance(hours=2)  # 12:30, same day
    await heartbeat.tick()
    assert len(schedule_calls(runner, "training")) == 1

    clock.advance(hours=24)  # next day 12:30
    await heartbeat.tick()
    assert len(schedule_calls(runner, "training")) == 2


async def test_weekly_fires_only_on_listed_days() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, _, runner = build(
        [agent("review", kind="weekly", time="08:00", days=["tue"])], clock=clock
    )

    await heartbeat.tick()  # Monday 09:00: time passed but wrong day
    assert schedule_calls(runner, "review") == []

    clock.advance(hours=24)  # Tuesday 09:00
    await heartbeat.tick()
    assert len(schedule_calls(runner, "review")) == 1

    clock.advance(hours=24)  # Wednesday 09:00
    await heartbeat.tick()
    assert len(schedule_calls(runner, "review")) == 1


async def test_interval_cadence() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, _, runner = build(
        [agent("pulse", kind="interval", every_minutes=60)], clock=clock
    )

    await heartbeat.tick()  # no prior run: fires immediately
    assert len(schedule_calls(runner, "pulse")) == 1

    clock.advance(minutes=30)
    await heartbeat.tick()
    assert len(schedule_calls(runner, "pulse")) == 1

    clock.advance(minutes=31)  # 61 minutes since last fire
    await heartbeat.tick()
    assert len(schedule_calls(runner, "pulse")) == 2


async def test_check_in_cadence_forming_vs_maintenance() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, _, runner = build(
        [
            agent("newbie", lifecycle=Lifecycle.FORMING),
            agent("steady", lifecycle=Lifecycle.MAINTENANCE),
        ],
        clock=clock,
    )

    await heartbeat.tick()  # no prior state: both fire
    assert len(check_in_calls(runner, "newbie")) == 1
    assert len(check_in_calls(runner, "steady")) == 1

    clock.advance(hours=25)  # past 1 day, well short of 7
    await heartbeat.tick()
    assert len(check_in_calls(runner, "newbie")) == 2
    assert len(check_in_calls(runner, "steady")) == 1

    clock.advance(days=7)
    await heartbeat.tick()
    assert len(check_in_calls(runner, "steady")) == 2


async def test_paused_and_retired_agents_get_no_jobs() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, _, runner = build(
        [
            agent("napper", lifecycle=Lifecycle.PAUSED, kind="interval", every_minutes=1),
            agent("gone", lifecycle=Lifecycle.RETIRED, kind="daily", time="00:00"),
        ],
        clock=clock,
    )

    fired = await heartbeat.tick()
    assert all(t.agent == "" for t in fired)  # only the reflection job
    assert all(t.agent == "" for t in runner.calls)


def test_in_quiet_hours_window_logic() -> None:
    def at(hour: int, minute: int) -> datetime:
        return datetime(2026, 1, 5, hour, minute, tzinfo=timezone.utc)

    spec = "22:30-07:30"  # crosses midnight
    assert in_quiet_hours(at(23, 0), spec)
    assert in_quiet_hours(at(6, 30), spec)
    assert in_quiet_hours(at(22, 30), spec)  # start inclusive
    assert not in_quiet_hours(at(7, 30), spec)  # end exclusive
    assert not in_quiet_hours(at(8, 0), spec)

    assert in_quiet_hours(at(13, 0), "12:00-14:00")
    assert not in_quiet_hours(at(15, 0), "12:00-14:00")
    assert not in_quiet_hours(at(13, 0), "")


async def test_quiet_hours_suppress_across_midnight() -> None:
    clock = FakeClock(datetime(2026, 1, 5, 23, 0, tzinfo=timezone.utc))
    heartbeat, _, runner = build(
        [agent("pulse", kind="interval", every_minutes=30)],
        clock=clock,
        quiet_hours="22:30-07:30",
    )

    assert await heartbeat.tick() == []  # 23:00 inside window
    clock.advance(hours=7, minutes=30)  # 06:30 next day, still inside
    assert await heartbeat.tick() == []
    assert runner.calls == []

    clock.advance(hours=1, minutes=30)  # 08:00, window over
    fired = await heartbeat.tick()
    assert any(t.agent == "pulse" and t.reason == "schedule" for t in fired)
    assert any(t.agent == "pulse" for t in runner.calls)


async def test_daily_job_inside_quiet_hours_catches_up_after_midnight() -> None:
    # A tracked daily job at 23:00 with quiet hours 22:30-07:30: suppressed
    # at 23:00, it must fire on the first tick after the window ends next
    # morning, not be lost until the schedule comes around again.
    store = MemoryStore()
    await store.put(
        Collections.HEARTBEAT,
        "schedule:journal",
        {"last": datetime(2026, 1, 4, 23, 0, tzinfo=timezone.utc).isoformat()},
    )
    clock = FakeClock(datetime(2026, 1, 5, 23, 0, tzinfo=timezone.utc))
    heartbeat, _, runner = build(
        [agent("journal", kind="daily", time="23:00")],
        clock=clock,
        store=store,
        quiet_hours="22:30-07:30",
    )

    assert await heartbeat.tick() == []  # 23:00, inside the window

    clock.advance(hours=9)  # 08:00 next day, window over
    fired = await heartbeat.tick()
    assert any(t.agent == "journal" and t.reason == "schedule" for t in fired)

    clock.advance(hours=2)  # 10:00 same day: yesterday's slot already served
    assert all(t.agent != "journal" for t in await heartbeat.tick())


async def test_misconfigured_schedule_warns_and_never_fires() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, _, runner = build(
        [
            agent("no-time", kind="daily"),
            agent("bad-days", kind="weekly", time="08:00", days=["someday"]),
        ],
        clock=clock,
    )
    fired = await heartbeat.tick()
    assert all(t.reason != "schedule" for t in fired)
    assert all(t.reason != "schedule" for t in runner.calls)


async def test_restart_does_not_double_fire() -> None:
    clock = FakeClock(MONDAY)
    clock.advance(hours=2)  # 11:00, past the daily time
    store = MemoryStore()

    heartbeat1, _, runner1 = build(
        [agent("training", kind="daily", time="10:00")], clock=clock, store=store
    )
    fired = await heartbeat1.tick()
    assert fired
    assert len(schedule_calls(runner1, "training")) == 1

    runner2 = RecordingRunner()
    heartbeat2, _, _ = build(
        [agent("training", kind="daily", time="10:00")],
        clock=clock,
        store=store,
        runner=runner2,
    )
    assert await heartbeat2.tick() == []
    assert runner2.calls == []


async def test_reflection_cadence() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, store, _ = build([], clock=clock, reflection_days=7)

    fired = await heartbeat.tick()  # no prior state: fires
    assert [t.reason for t in fired] == ["reflection"]
    assert fired[0].agent == ""
    assert await store.get(Collections.HEARTBEAT, "reflection") is not None

    clock.advance(days=1)
    assert await heartbeat.tick() == []

    clock.advance(days=7)
    fired = await heartbeat.tick()
    assert [t.reason for t in fired] == ["reflection"]


async def test_crashing_runner_does_not_block_others_or_hot_loop() -> None:
    clock = FakeClock(MONDAY)
    runner = FlakyRunner()
    heartbeat, store, _ = build(
        [
            agent("bad", kind="interval", every_minutes=60),
            agent("good", kind="interval", every_minutes=60),
        ],
        clock=clock,
        runner=runner,
    )

    fired = await heartbeat.tick()
    assert any(t.agent == "good" for t in runner.calls)  # ran despite the crash
    assert any(t.agent == "bad" for t in fired)  # attempted and reported
    assert await store.get(Collections.HEARTBEAT, "schedule:bad") is not None

    # Same instant: the crasher's last-run advanced, so nothing refires.
    assert await heartbeat.tick() == []
