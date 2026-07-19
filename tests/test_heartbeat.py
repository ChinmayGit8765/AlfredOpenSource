"""Heartbeat tests: cadences, quiet hours, restart safety, crash isolation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    # The roadmap nudge is its own concern; default it off so the other
    # cadence tests stay about the job they exercise. test_roadmap_nudge_*
    # enable it explicitly.
    config_kwargs.setdefault("roadmap_nudge_days", 0)
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


async def test_roadmap_nudge_fires_on_its_cadence() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, store, _ = build([], clock=clock, roadmap_nudge_days=1)

    fired = await heartbeat.tick()  # no prior state: fires
    assert any(t.reason == "roadmap_nudge" and t.agent == "" for t in fired)
    assert await store.get(Collections.HEARTBEAT, "roadmap_nudge") is not None

    clock.advance(hours=12)  # well short of a day
    assert all(t.reason != "roadmap_nudge" for t in await heartbeat.tick())

    clock.advance(days=1)
    fired = await heartbeat.tick()
    assert any(t.reason == "roadmap_nudge" for t in fired)


async def test_roadmap_nudge_disabled_when_days_is_zero() -> None:
    clock = FakeClock(MONDAY)
    heartbeat, _, _ = build([], clock=clock, roadmap_nudge_days=0)
    fired = await heartbeat.tick()
    assert all(t.reason != "roadmap_nudge" for t in fired)


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
    # fired reports what RAN; the crasher is retried later, not reported.
    assert all(t.agent != "bad" for t in fired)
    assert await store.get(Collections.HEARTBEAT, "schedule:bad") is not None

    # Same instant: the crasher is inside its backoff window, nothing refires.
    assert await heartbeat.tick() == []


async def test_transient_failure_retries_after_backoff_then_succeeds_once() -> None:
    clock = FakeClock(MONDAY)

    class FailsOnce(RecordingRunner):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def __call__(self, trigger: ScheduledTrigger) -> None:
            if trigger.reason == "schedule" and not self.failed:
                self.failed = True
                raise RuntimeError("model still booting")
            await super().__call__(trigger)

    runner = FailsOnce()
    heartbeat, _, _ = build(
        [agent("training", kind="interval", every_minutes=240)],
        clock=clock,
        runner=runner,
        tick_seconds=60,
    )

    fired = await heartbeat.tick()
    assert all(t.reason != "schedule" for t in fired)  # first attempt failed
    clock.advance(minutes=2)  # past the first backoff (one tick)
    fired = await heartbeat.tick()
    assert [t.agent for t in fired if t.reason == "schedule"] == ["training"]
    assert len(schedule_calls(runner, "training")) == 1

    # The retried occurrence is consumed: no double fire afterwards.
    clock.advance(minutes=2)
    assert await heartbeat.tick() == []


async def test_repeated_failure_abandons_occurrence_at_cap() -> None:
    clock = FakeClock(MONDAY)
    runner = FlakyRunner()
    heartbeat, store, _ = build(
        [agent("bad", kind="interval", every_minutes=60)],
        clock=clock,
        runner=runner,
        tick_seconds=60,
    )

    for _ in range(3):  # cap is 3 attempts per occurrence
        await heartbeat.tick()
        clock.advance(minutes=10)

    doc = await store.get(Collections.HEARTBEAT, "schedule:bad")
    assert doc is not None and "last" in doc  # abandoned: last advanced
    # Within the interval nothing refires; the failure spiral is over.
    assert await heartbeat.tick() == []


async def test_store_put_failure_does_not_refire_or_abort_tick() -> None:
    clock = FakeClock(MONDAY)

    class HeartbeatWriteFails(MemoryStore):
        async def put(self, collection: str, key: str, doc: dict) -> None:
            if collection == Collections.HEARTBEAT:
                raise RuntimeError("disk full")
            await super().put(collection, key, doc)

    store = HeartbeatWriteFails()
    heartbeat, _, runner = build(
        [
            agent("alpha", kind="interval", every_minutes=60),
            agent("beta", kind="interval", every_minutes=60),
        ],
        clock=clock,
        store=store,
    )

    fired = await heartbeat.tick()
    # Both jobs ran despite the store failing to persist their last-run.
    assert {t.agent for t in fired if t.reason == "schedule"} == {"alpha", "beta"}
    # Same process: the in-memory bridge suppresses a duplicate fire.
    assert await heartbeat.tick() == []
    assert len([t for t in runner.calls if t.reason == "schedule"]) == 2


async def test_catchup_fires_in_scheduled_time_order_not_alphabetical() -> None:
    # zulu plans at 08:00, alpha audits at 09:30: alphabetical order would
    # run the auditor before the planner on a cold 10:00 start.
    clock = FakeClock(MONDAY.replace(hour=10, minute=0))
    heartbeat, _, runner = build(
        [
            agent("alpha", kind="weekly", days=["mon"], time="09:30"),
            agent("zulu", kind="weekly", days=["mon"], time="08:00"),
        ],
        clock=clock,
    )

    await heartbeat.tick()
    scheduled = [t.agent for t in runner.calls if t.reason == "schedule"]
    assert scheduled == ["zulu", "alpha"]


async def test_cold_start_check_ins_fire_after_caught_up_schedules() -> None:
    clock = FakeClock(MONDAY.replace(hour=10, minute=0))
    heartbeat, _, runner = build(
        [agent("alpha", lifecycle=Lifecycle.FORMING, kind="weekly", days=["mon"], time="08:00")],
        clock=clock,
    )

    await heartbeat.tick()
    reasons = [t.reason for t in runner.calls if t.agent == "alpha"]
    assert reasons == ["schedule", "check_in"]


async def test_retention_sweep_prunes_old_logs_and_spares_recent() -> None:
    clock = FakeClock(MONDAY)
    store = MemoryStore()
    old = (MONDAY - timedelta(days=120)).isoformat()
    await store.append(Collections.MESSAGES, {"text": "ancient", "at": old})
    await store.append(Collections.AUDIT, {"event": "ancient", "at": old})
    await store.append(
        Collections.MESSAGES, {"text": "fresh", "at": MONDAY.isoformat()}
    )
    await store.append(Collections.AUDIT, {"event": "undated"})

    heartbeat, _, _ = build([], clock=clock, store=store, retention_days=90)
    await heartbeat.tick()

    messages = await store.query(Collections.MESSAGES)
    audit = await store.query(Collections.AUDIT)
    assert [m["text"] for m in messages] == ["fresh"]
    # Undated rows are never deleted; only the dated ancient row went.
    assert [a["event"] for a in audit] == ["undated"]


async def test_retention_disabled_by_default_keeps_everything() -> None:
    clock = FakeClock(MONDAY)
    store = MemoryStore()
    ancient = (MONDAY.replace(year=2020)).isoformat()
    await store.append(Collections.AUDIT, {"event": "ancient", "at": ancient})

    heartbeat, _, _ = build([], clock=clock, store=store)
    await heartbeat.tick()

    assert len(await store.query(Collections.AUDIT)) == 1
