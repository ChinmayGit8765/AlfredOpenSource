"""LocalToolAdapter: ALFRED's built-in tools.

The floor of the action layer: a handful of tools over the store and the
clock that every agent can be granted safely. Specs carry honest JSON
schemas so the model knows exactly what each tool accepts, and tiers so
the dispatcher can gate correctly. Argument validation happens here with
private pydantic models; bad arguments become failed results, never
exceptions, because an LLM caller needs feedback it can act on.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from alfred.domain.schemas import Collections, Observation, Outcome, Plan, UserProfile
from alfred.errors import ToolNotFoundError
from alfred.ports.clock import ClockPort
from alfred.ports.store import StorePort
from alfred.ports.tools import CapabilityTier, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

# Documented in schemas.Collections: the current profile lives at this key.
_PROFILE_KEY = "current"

_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_LIST_PLANS_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {
            "type": "string",
            "description": "Only plans created by this agent.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "default": 5,
            "description": "Maximum number of plans to return.",
        },
    },
    "additionalProperties": False,
}

_LIST_OUTCOMES_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {
            "type": "string",
            "description": "Only outcomes logged for this agent.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 10,
            "description": "Maximum number of outcomes to return.",
        },
    },
    "additionalProperties": False,
}

_LOG_NOTE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "description": "The note to record about the owner or the day.",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ListPlansArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class _ListOutcomesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class _LogNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


def _without_key(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != "_key"}


_Handler = Callable[[Any], Awaitable[Any]]


class LocalToolAdapter:
    """ToolPort implementation of ALFRED's built-in tools."""

    def __init__(self, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock
        # Insertion order here is the stable order list_tools() returns.
        self._tools: dict[str, tuple[ToolSpec, type[BaseModel], _Handler]] = {
            "current_time": (
                ToolSpec(
                    name="current_time",
                    description="Current local date and time (ISO) plus the weekday name.",
                    parameters=_NO_PARAMS,
                    tier=CapabilityTier.READ_ONLY,
                    source="local",
                ),
                _NoArgs,
                self._current_time,
            ),
            "list_plans": (
                ToolSpec(
                    name="list_plans",
                    description=(
                        "Recent stored plans, newest first, optionally filtered "
                        "by agent. Returns compact summaries."
                    ),
                    parameters=_LIST_PLANS_PARAMS,
                    tier=CapabilityTier.READ_ONLY,
                    source="local",
                ),
                _ListPlansArgs,
                self._list_plans,
            ),
            "list_recent_outcomes": (
                ToolSpec(
                    name="list_recent_outcomes",
                    description=(
                        "Recent plan outcomes, newest first, optionally filtered "
                        "by agent."
                    ),
                    parameters=_LIST_OUTCOMES_PARAMS,
                    tier=CapabilityTier.READ_ONLY,
                    source="local",
                ),
                _ListOutcomesArgs,
                self._list_recent_outcomes,
            ),
            "list_agents_state": (
                ToolSpec(
                    name="list_agents_state",
                    description="Per-agent adherence stats from the owner's profile.",
                    parameters=_NO_PARAMS,
                    tier=CapabilityTier.READ_ONLY,
                    source="local",
                ),
                _NoArgs,
                self._list_agents_state,
            ),
            "log_note": (
                ToolSpec(
                    name="log_note",
                    description=(
                        "Append a note about the owner to the observation log. "
                        "Returns the stored key."
                    ),
                    parameters=_LOG_NOTE_PARAMS,
                    tier=CapabilityTier.REVERSIBLE_WRITE,
                    source="local",
                ),
                _LogNoteArgs,
                self._log_note,
            ),
        }

    async def list_tools(self) -> list[ToolSpec]:
        return [spec for spec, _, _ in self._tools.values()]

    async def invoke(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            raise ToolNotFoundError(f"unknown tool: {name}")
        _, args_model, handler = entry
        try:
            parsed = args_model.model_validate(dict(args))
        except ValidationError as exc:
            # Invalid arguments are feedback for the calling model, not a fault.
            return ToolResult(ok=False, error=f"invalid arguments for {name}: {exc}")
        try:
            content = await handler(parsed)
        except Exception as exc:
            logger.warning("local tool %s failed: %s", name, exc)
            return ToolResult(ok=False, error=f"{name} failed: {exc}")
        return ToolResult(ok=True, content=content)

    async def _current_time(self, args: _NoArgs) -> dict[str, str]:
        now = self._clock.now()
        return {"iso": now.isoformat(), "weekday": now.strftime("%A")}

    async def _list_plans(self, args: _ListPlansArgs) -> list[dict[str, Any]]:
        where = {"agent": args.agent} if args.agent else None
        docs = await self._store.query(
            Collections.PLANS, where=where, limit=args.limit, newest_first=True
        )
        summaries: list[dict[str, Any]] = []
        for doc in docs:
            plan = Plan.model_validate(_without_key(doc))
            titles_by_day: dict[str, list[str]] = {}
            for item in plan.items:
                titles_by_day.setdefault(item.day or "unscheduled", []).append(item.title)
            summaries.append(
                {
                    "id": plan.id,
                    "agent": plan.agent,
                    "week_of": plan.week_of.isoformat() if plan.week_of else None,
                    "item_count": len(plan.items),
                    "total_load": plan.total_load,
                    "titles_by_day": titles_by_day,
                }
            )
        return summaries

    async def _list_recent_outcomes(self, args: _ListOutcomesArgs) -> list[dict[str, Any]]:
        where = {"agent": args.agent} if args.agent else None
        docs = await self._store.query(
            Collections.OUTCOMES, where=where, limit=args.limit, newest_first=True
        )
        return [
            Outcome.model_validate(_without_key(doc)).model_dump(mode="json")
            for doc in docs
        ]

    async def _list_agents_state(self, args: _NoArgs) -> dict[str, dict[str, Any]]:
        doc = await self._store.get(Collections.PROFILE, _PROFILE_KEY)
        if doc is None:
            return {}
        profile = UserProfile.model_validate(_without_key(doc))
        return {
            agent: {
                **stats.model_dump(mode="json"),
                "total": stats.total,
                "rate": round(stats.rate, 3),
            }
            for agent, stats in profile.adherence.items()
        }

    async def _log_note(self, args: _LogNoteArgs) -> dict[str, str]:
        observation = Observation(
            source="tool", kind="event", text=args.text, at=self._clock.now()
        )
        key = await self._store.append(
            Collections.OBSERVATIONS, observation.model_dump(mode="json")
        )
        return {"key": key}
