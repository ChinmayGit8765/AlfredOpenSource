"""The Conductor: concurrent plans that do not collide.

Multiple agents plan independently; the Conductor is the layer that makes
them one coherent week. detect_conflicts is the pure detector (capacity
and calendar math, no model). Conductor.reconcile lets the model resolve
genuine conflicts but verifies the result and falls back to a
deterministic pruner, so a reconciled schedule is never over capacity
regardless of what the model says.
"""

from __future__ import annotations

import logging
from datetime import date

from alfred.domain.schemas import (
    Adjustment,
    Conflict,
    Plan,
    PlanItem,
    ReconciledSchedule,
    UserProfile,
)
from alfred.domain.structured import structured_call
from alfred.errors import StructuredCallError
from alfred.ports.clock import ClockPort
from alfred.ports.model import ModelPort

logger = logging.getLogger(__name__)

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_OVERLOAD_KINDS = frozenset({"overload_week", "overload_day"})

_SYSTEM_PROMPT = (
    "You are the Conductor of a personal optimization system. Several agents "
    "produced plans for the same week and the plans collide. Resolve the "
    "listed conflicts by moving, shrinking, or dropping items. Never invent "
    "new commitments. Never increase any item's load. Prefer moving over "
    "shrinking, and shrinking over dropping. Respect the owner's stated "
    "constraints and preferences. Record every change you make as an "
    "Adjustment with an honest detail string. Fill warnings whenever the "
    "owner should know something about the trade-offs you made. Reply with a "
    "single JSON object matching the ReconciledSchedule schema."
)


def _daily_capacity(weekly_capacity: int) -> int:
    # Integer ceil(weekly_capacity / 5): five working days share the week.
    return (weekly_capacity + 4) // 5


def _normalize_day(day: str | None) -> str | None:
    """Map any day spelling to "mon".."sun", or None when unusable.

    Accepts weekday names in any case (3-letter prefix wins) and ISO dates,
    which map to their weekday. Anything else counts toward the week only.
    """
    if day is None:
        return None
    text = day.strip()
    if not text:
        return None
    try:
        return _WEEKDAYS[date.fromisoformat(text).weekday()]
    except ValueError:
        pass
    prefix = text[:3].lower()
    return prefix if prefix in _WEEKDAYS else None


def _parse_time(value: str | None) -> int | None:
    """Parse "HH:MM" to minutes since midnight; None when absent or junk."""
    if value is None:
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def _agent_name(plan: Plan) -> str:
    return plan.agent or "unnamed"


def detect_conflicts(plans: list[Plan], weekly_capacity: int) -> list[Conflict]:
    """Pure conflict detection across concurrent plans.

    Plans are grouped by week_of first: plans for different weeks never
    contend for the same capacity. Within each week:
    overload_week: total load strictly above weekly_capacity.
    overload_day: load on one weekday strictly above ceil(weekly_capacity/5).
    time_collision: two timed items on the same day with overlapping
    [start, start+duration) windows (missing duration assumed 60 minutes).
    """
    groups: dict[date | None, list[Plan]] = {}
    for plan in plans:
        groups.setdefault(plan.week_of, []).append(plan)
    conflicts: list[Conflict] = []
    for week in sorted(groups, key=lambda w: (w is not None, w or date.min)):
        conflicts.extend(_detect_week_conflicts(groups[week], weekly_capacity))
    return conflicts


def _detect_week_conflicts(plans: list[Plan], weekly_capacity: int) -> list[Conflict]:
    conflicts: list[Conflict] = []
    entries: list[tuple[Plan, PlanItem, str | None]] = [
        (plan, item, _normalize_day(item.day)) for plan in plans for item in plan.items
    ]

    total = sum(item.load for _, item, _ in entries)
    if total > weekly_capacity:
        agents = sorted({_agent_name(plan) for plan, _, _ in entries})
        conflicts.append(
            Conflict(
                kind="overload_week",
                detail=(
                    f"total load {total} exceeds weekly capacity "
                    f"{weekly_capacity} (agents: {', '.join(agents)})"
                ),
                item_ids=[item.id for _, item, _ in entries],
            )
        )

    daily_cap = _daily_capacity(weekly_capacity)
    by_day: dict[str, list[tuple[Plan, PlanItem]]] = {}
    for plan, item, day in entries:
        if day is not None:
            by_day.setdefault(day, []).append((plan, item))

    for day in _WEEKDAYS:
        day_items = by_day.get(day, [])
        if not day_items:
            continue
        load = sum(item.load for _, item in day_items)
        if load > daily_cap:
            names = ", ".join(
                f"{_agent_name(plan)}/{item.title}" for plan, item in day_items
            )
            conflicts.append(
                Conflict(
                    kind="overload_day",
                    day=day,
                    detail=(
                        f"load {load} on {day} exceeds daily capacity "
                        f"{daily_cap} ({names})"
                    ),
                    item_ids=[item.id for _, item in day_items],
                )
            )

    for day in _WEEKDAYS:
        timed: list[tuple[int, int, Plan, PlanItem]] = []
        for plan, item in by_day.get(day, []):
            start = _parse_time(item.time)
            if start is None:
                continue
            duration = item.duration_min if item.duration_min is not None else 60
            timed.append((start, start + duration, plan, item))
        timed.sort(key=lambda entry: (entry[0], entry[1], entry[3].id))
        for i in range(len(timed)):
            for j in range(i + 1, len(timed)):
                s1, e1, p1, i1 = timed[i]
                s2, e2, p2, i2 = timed[j]
                # Half-open windows: back-to-back items never collide.
                if max(s1, s2) < min(e1, e2):
                    conflicts.append(
                        Conflict(
                            kind="time_collision",
                            day=day,
                            detail=(
                                f"{_agent_name(p1)}/{i1.title} at {i1.time} "
                                f"overlaps {_agent_name(p2)}/{i2.title} "
                                f"at {i2.time} on {day}"
                            ),
                            item_ids=[i1.id, i2.id],
                        )
                    )

    return conflicts


def _earliest_week_of(plans: list[Plan]) -> date | None:
    weeks = [plan.week_of for plan in plans if plan.week_of is not None]
    return min(weeks) if weeks else None


def _render_user(
    plans: list[Plan],
    capacity: int,
    profile: UserProfile,
    conflicts: list[Conflict],
) -> str:
    lines = [
        f"Weekly capacity: {capacity} points; "
        f"daily capacity: {_daily_capacity(capacity)} points."
    ]
    if profile.constraints:
        lines.append("Owner constraints: " + "; ".join(profile.constraints))
    if profile.preferences:
        lines.append("Owner preferences: " + "; ".join(profile.preferences))
    lines.append("")
    lines.append("Plans:")
    lines.extend(plan.model_dump_json() for plan in plans)
    lines.append("")
    lines.append("Detected conflicts:")
    for conflict in conflicts:
        where = f" [{conflict.day}]" if conflict.day else ""
        lines.append(f"- {conflict.kind}{where}: {conflict.detail}")
    return "\n".join(lines)


class Conductor:
    """Reconciles concurrent plans into one schedule that fits capacity."""

    def __init__(self, model: ModelPort, clock: ClockPort) -> None:
        self._model = model
        self._clock = clock

    async def reconcile(
        self, plans: list[Plan], profile: UserProfile
    ) -> ReconciledSchedule:
        capacity = profile.weekly_capacity
        conflicts = detect_conflicts(plans, capacity)
        week_of = _earliest_week_of(plans)
        total = sum(plan.total_load for plan in plans)

        if not conflicts:
            summary = (
                "no plans to reconcile"
                if not plans
                else (
                    f"{len(plans)} plan(s) fit within weekly capacity "
                    f"{capacity} (total load {total}); no conflicts"
                )
            )
            return ReconciledSchedule(
                week_of=week_of,
                plans=list(plans),
                adjustments=[],
                total_load=total,
                warnings=[],
                summary=summary,
            )

        try:
            proposed = await structured_call(
                self._model,
                schema=ReconciledSchedule,
                system=_SYSTEM_PROMPT,
                user=_render_user(plans, capacity, profile, conflicts),
            )
        except StructuredCallError:
            logger.warning("conductor: model produced no valid resolution")
            return self._fallback(
                plans, capacity, week_of, "model produced no valid resolution"
            )

        # Backstop: never trust the model's arithmetic or its inventory.
        known_ids = {item.id for plan in plans for item in plan.items}
        proposed_id_list = [
            item.id for plan in proposed.plans for item in plan.items
        ]
        proposed_ids = set(proposed_id_list)
        invented = sorted(proposed_ids - known_ids)
        duplicated = sorted(
            {i for i in proposed_ids if proposed_id_list.count(i) > 1}
        )
        remaining = [
            c
            for c in detect_conflicts(proposed.plans, capacity)
            if c.kind in _OVERLOAD_KINDS
        ]
        if invented or duplicated or remaining:
            reasons: list[str] = []
            if remaining:
                reasons.append("model resolution still exceeds capacity")
            if invented:
                reasons.append(f"model invented unknown items: {', '.join(invented)}")
            if duplicated:
                reasons.append(f"model duplicated items: {', '.join(duplicated)}")
            logger.warning("conductor: %s; engaging fallback", "; ".join(reasons))
            return self._fallback(plans, capacity, week_of, "; ".join(reasons))

        proposed.week_of = week_of
        proposed.total_load = sum(plan.total_load for plan in proposed.plans)
        if not proposed.summary:
            proposed.summary = (
                f"resolved {len(conflicts)} conflict(s) across {len(plans)} plan(s)"
            )
        return proposed

    def _fallback(
        self,
        plans: list[Plan],
        capacity: int,
        week_of: date | None,
        reason: str,
    ) -> ReconciledSchedule:
        """Deterministic pruner: drop lowest-load items until within capacity.

        Works on the original plans, never the model output, so invented
        items can never survive. Weeks prune independently. Most overloaded
        day first (ties: earliest weekday), lowest positive-load item first
        (ties: alphabetical item id); zero-load items are never dropped
        because removing them cannot reduce overload.
        """
        daily_cap = _daily_capacity(capacity)
        working = [plan.model_copy(deep=True) for plan in plans]
        adjustments: list[Adjustment] = []

        groups: dict[date | None, list[Plan]] = {}
        for plan in working:
            groups.setdefault(plan.week_of, []).append(plan)

        for group in groups.values():
            self._prune_week(group, capacity, daily_cap, adjustments)

        total = sum(plan.total_load for plan in working)
        dropped = len(adjustments)
        return ReconciledSchedule(
            week_of=week_of,
            plans=working,
            adjustments=adjustments,
            total_load=total,
            warnings=[
                f"deterministic fallback engaged: {reason}; "
                f"dropped {dropped} item(s) to fit capacity {capacity}"
            ],
            summary=(
                f"deterministic fallback dropped {dropped} item(s) to fit "
                f"weekly capacity {capacity}"
            ),
        )

    def _prune_week(
        self,
        group: list[Plan],
        capacity: int,
        daily_cap: int,
        adjustments: list[Adjustment],
    ) -> None:
        while True:
            items = [(plan, item) for plan in group for item in plan.items]
            total = sum(item.load for _, item in items)
            day_loads: dict[str, int] = {}
            for _, item in items:
                day = _normalize_day(item.day)
                if day is not None:
                    day_loads[day] = day_loads.get(day, 0) + item.load
            overloaded = [d for d, load in day_loads.items() if load > daily_cap]
            if not overloaded and total <= capacity:
                break

            if overloaded:
                target = min(
                    overloaded,
                    key=lambda d: (-(day_loads[d] - daily_cap), _WEEKDAYS.index(d)),
                )
                candidates = [
                    (plan, item)
                    for plan, item in items
                    if _normalize_day(item.day) == target
                ]
            else:
                candidates = items

            droppable = [(plan, item) for plan, item in candidates if item.load > 0]
            if not droppable:
                # Cannot happen while overloaded (overload implies positive
                # load), but a guard beats an infinite loop if it ever does.
                logger.warning("conductor fallback: no droppable items remain")
                break

            plan, victim = min(droppable, key=lambda pi: (pi[1].load, pi[1].id))
            for index, item in enumerate(plan.items):
                if item is victim:
                    del plan.items[index]
                    break
            adjustments.append(
                Adjustment(
                    agent=_agent_name(plan),
                    item_id=victim.id,
                    action="drop",
                    detail=(
                        f"dropped '{victim.title}' (load {victim.load}, "
                        f"day {_normalize_day(victim.day) or 'unscheduled'}) "
                        f"to fit capacity"
                    ),
                )
            )
