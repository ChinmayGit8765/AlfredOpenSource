"""In-memory implementations of every port.

These are real, behaviour-complete implementations (not mocks), so domain
tests exercise genuine logic end to end without I/O. They also back the
CLI's --fake mode for offline dry runs.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from alfred.errors import ToolNotFoundError
from alfred.ports.model import ModelMessage, ModelOptions
from alfred.ports.tools import CapabilityTier, ToolResult, ToolSpec
from alfred.ports.transport import OutboundMessage

Responder = Callable[[Sequence[ModelMessage]], str]


class FakeModel:
    """ModelPort fake fed with scripted responses.

    Each entry is either a literal string or a callable receiving the
    messages and returning a string. When the script is exhausted the
    last entry repeats, which keeps retry-loop tests simple.
    """

    def __init__(self, responses: Sequence[str | Responder] | None = None) -> None:
        self.responses: list[str | Responder] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def push(self, response: str | Responder) -> None:
        self.responses.append(response)

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        json_schema: Mapping[str, Any] | None = None,
        options: ModelOptions | None = None,
    ) -> str:
        index = min(len(self.calls), len(self.responses) - 1)
        if index < 0:
            raise AssertionError("FakeModel has no scripted responses")
        self.calls.append(
            {
                "messages": list(messages),
                "json_schema": dict(json_schema) if json_schema else None,
                "options": options,
            }
        )
        entry = self.responses[index]
        return entry(messages) if callable(entry) else entry


class MemoryStore:
    """StorePort fake: dict-backed document store with time-ordered appends."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._counter = itertools.count(1)

    def _collection(self, name: str) -> dict[str, dict[str, Any]]:
        return self._data.setdefault(name, {})

    async def put(self, collection: str, key: str, doc: Mapping[str, Any]) -> None:
        self._collection(collection)[key] = dict(doc)

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        doc = self._collection(collection).get(key)
        if doc is None:
            return None
        return {**doc, "_key": key}

    async def delete(self, collection: str, key: str) -> bool:
        return self._collection(collection).pop(key, None) is not None

    async def append(self, collection: str, doc: Mapping[str, Any]) -> str:
        # Zero-padded counter keys sort chronologically, matching the
        # time-ordered key contract of StorePort.append.
        key = f"{next(self._counter):012d}"
        self._collection(collection)[key] = dict(doc)
        return key

    async def query(
        self,
        collection: str,
        *,
        where: Mapping[str, Any] | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        items = sorted(self._collection(collection).items(), reverse=newest_first)
        results: list[dict[str, Any]] = []
        for key, doc in items:
            if where and any(doc.get(k) != v for k, v in where.items()):
                continue
            results.append({**doc, "_key": key})
            if limit is not None and len(results) >= limit:
                break
        return results


class FakeClock:
    """ClockPort fake with settable time; sleep advances instantly."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        await asyncio.sleep(0)  # yield so cooperative loops interleave


class CapturingTransport:
    """TransportPort fake that records every outbound message."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)


class FakeTools:
    """ToolPort fake with registrable tools and an invocation log."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Callable[..., Any]]] = {}
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def add(
        self,
        name: str,
        tier: CapabilityTier = CapabilityTier.READ_ONLY,
        handler: Callable[..., Any] | None = None,
        description: str = "",
        source: str = "local",
    ) -> None:
        spec = ToolSpec(
            name=name, description=description or name, tier=tier, source=source
        )
        self._tools[name] = (spec, handler or (lambda **kwargs: {"ok": True}))

    async def list_tools(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    async def invoke(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        if name not in self._tools:
            raise ToolNotFoundError(f"Unknown tool: {name}")
        self.invocations.append((name, dict(args)))
        _, handler = self._tools[name]
        try:
            result = handler(**dict(args))
            if asyncio.iscoroutine(result):
                result = await result
            return ToolResult(ok=True, content=result)
        except Exception as exc:  # tool faults become results, not crashes
            return ToolResult(ok=False, error=str(exc))
