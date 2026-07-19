"""Tests for alfred.adapters.mcp_tools: MCP and composite tool adapters.

No subprocesses: adapters are seeded with stubbed session objects carrying
the same shape connect() would have built, and reconnection is driven by an
injected connector plus a controllable monotonic clock.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from alfred.adapters.mcp_tools import CompositeToolAdapter, McpToolAdapter
from alfred.config import McpServerConfig
from alfred.errors import ToolNotFoundError
from alfred.ports.tools import CapabilityTier, ToolSpec
from alfred.testing import FakeTools


class StubSession:
    """Just enough of mcp.ClientSession for invoke(): call_tool only."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._results = results or {}

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, arguments or {}))
        result = self._results[name]
        if isinstance(result, Exception):
            raise result
        return result


class ClosedResourceError(Exception):
    """Stand-in for anyio.ClosedResourceError.

    The adapter matches transport faults by exception class name so that it
    never has to import anyio; sharing the name is the whole point.
    """


class FakeClock:
    """Injectable monotonic source so cooldown windows are deterministic."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeConnector:
    """Scripted connector: yields (session, raw tools) per attempt, else fails."""

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.calls = 0
        self._outcomes = list(outcomes or [])

    async def __call__(self, config: McpServerConfig, stack: Any) -> tuple[Any, list[Any]]:
        self.calls += 1
        # Yield control so concurrent callers genuinely queue on the lock.
        await asyncio.sleep(0)
        if not self._outcomes:
            raise ConnectionError("no server to connect to")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def call_result(*blocks: SimpleNamespace, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), isError=is_error)


def server_config(name: str = "cal", tiers: dict[str, str] | None = None) -> McpServerConfig:
    return McpServerConfig(name=name, command="fake-server", tool_tiers=tiers or {})


def mcp_tool(name: str, description: str | None = None, schema: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, inputSchema=schema)


def spec(name: str, tier: CapabilityTier = CapabilityTier.READ_ONLY) -> ToolSpec:
    server = name.partition(".")[0]
    return ToolSpec(name=name, description=name, tier=tier, source=f"mcp:{server}")


def seeded_adapter(
    sessions: dict[str, Any],
    specs: list[ToolSpec],
    *,
    connector: Any = None,
    monotonic: Any = None,
) -> McpToolAdapter:
    """Build an adapter in the connected state without spawning processes.

    Poking the per-server state is deliberate: it is the narrowest way to
    simulate "connect() succeeded earlier" now that the constructor takes
    configs instead of prebuilt sessions. The default connector always
    fails so no test accidentally reaches the real mcp stack.
    """
    adapter = McpToolAdapter(
        servers=[server_config(name) for name in sessions],
        connector=connector if connector is not None else FakeConnector(),
        monotonic=monotonic,
    )
    for name, session in sessions.items():
        state = adapter._states[name]
        state.session = session
        state.specs = [s for s in specs if s.name.partition(".")[0] == name]
    return adapter


# ---------------------------------------------------------------------------
# Spec mapping: namespacing and tiers
# ---------------------------------------------------------------------------


def test_specs_namespaced_and_described():
    config = server_config("cal", tiers={"list_events": "read_only"})
    tools = [mcp_tool("list_events", "List calendar events", {"type": "object"})]

    [built] = McpToolAdapter._specs_for_server(config, tools)

    assert built.name == "cal.list_events"
    assert built.description == "List calendar events"
    assert built.parameters == {"type": "object"}
    assert built.source == "mcp:cal"


def test_classified_tool_gets_configured_tier():
    config = server_config(
        "cal", tiers={"list_events": "read_only", "create_event": "reversible_write"}
    )
    tools = [mcp_tool("list_events"), mcp_tool("create_event")]

    built = McpToolAdapter._specs_for_server(config, tools)
    tiers = {s.name: s.tier for s in built}

    assert tiers["cal.list_events"] == CapabilityTier.READ_ONLY
    assert tiers["cal.create_event"] == CapabilityTier.REVERSIBLE_WRITE


def test_unclassified_tool_defaults_to_destructive():
    config = server_config("cal")
    [built] = McpToolAdapter._specs_for_server(config, [mcp_tool("wipe_calendar")])
    assert built.tier == CapabilityTier.DESTRUCTIVE


def test_invalid_tier_string_defaults_to_destructive():
    config = server_config("cal", tiers={"thing": "harmless"})
    [built] = McpToolAdapter._specs_for_server(config, [mcp_tool("thing")])
    assert built.tier == CapabilityTier.DESTRUCTIVE


def test_missing_description_and_schema_default_to_empty():
    config = server_config("cal")
    [built] = McpToolAdapter._specs_for_server(config, [mcp_tool("thing", None, None)])
    assert built.description == ""
    assert built.parameters == {}


# ---------------------------------------------------------------------------
# statuses: the doctor's snapshot
# ---------------------------------------------------------------------------


def test_statuses_reports_tools_and_names_the_unclassified():
    config = McpServerConfig(
        name="cal",
        command="fake-server",
        # One classified by bare name, one by qualified name: both count,
        # mirroring how _tier_for resolves them at dispatch time.
        tool_tiers={"list_events": "read_only", "cal.create_event": "reversible_write"},
    )
    adapter = McpToolAdapter(servers=[config], connector=FakeConnector())
    state = adapter._states["cal"]
    state.session = StubSession()
    state.specs = McpToolAdapter._specs_for_server(
        config,
        [mcp_tool("list_events"), mcp_tool("create_event"), mcp_tool("wipe_all")],
    )

    [status] = adapter.statuses()

    assert status.name == "cal"
    assert status.connected is True
    assert len(status.specs) == 3
    assert status.unclassified == ("wipe_all",)


def test_statuses_marks_dead_server_disconnected():
    adapter = McpToolAdapter(servers=[server_config("cal")], connector=FakeConnector())

    [status] = adapter.statuses()

    assert status.connected is False
    assert status.specs == ()


# ---------------------------------------------------------------------------
# invoke: routing and result mapping
# ---------------------------------------------------------------------------


def make_adapter() -> tuple[McpToolAdapter, StubSession, StubSession]:
    cal = StubSession(
        {
            "list_events": call_result(
                text_block("9am standup"), text_block("2pm gym")
            ),
            "delete_event": call_result(
                text_block("no such event"), is_error=True
            ),
            "broken": RuntimeError("pipe closed"),
        }
    )
    mail = StubSession({"send": call_result(text_block("sent"))})
    specs = [
        spec("cal.list_events"),
        spec("cal.delete_event", CapabilityTier.DESTRUCTIVE),
        spec("cal.broken"),
        spec("mail.send", CapabilityTier.REVERSIBLE_WRITE),
    ]
    adapter = seeded_adapter({"cal": cal, "mail": mail}, specs)
    return adapter, cal, mail


async def test_list_tools_returns_built_specs():
    adapter, _, _ = make_adapter()
    names = [s.name for s in await adapter.list_tools()]
    assert names == ["cal.list_events", "cal.delete_event", "cal.broken", "mail.send"]


async def test_invoke_routes_to_right_session_with_bare_name():
    adapter, cal, mail = make_adapter()

    result = await adapter.invoke("mail.send", {"to": "owner"})

    assert result.ok is True
    assert mail.calls == [("send", {"to": "owner"})]
    assert cal.calls == []


async def test_invoke_concatenates_text_content():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("cal.list_events", {})
    assert result.ok is True
    assert result.content == "9am standup\n2pm gym"


async def test_invoke_ignores_non_text_content():
    session = StubSession(
        {
            "shot": SimpleNamespace(
                content=[
                    SimpleNamespace(type="image", data="abc", mimeType="image/png"),
                    text_block("here you go"),
                ],
                isError=False,
            )
        }
    )
    adapter = seeded_adapter({"cam": session}, [spec("cam.shot")])
    result = await adapter.invoke("cam.shot", {})
    assert result.ok is True
    assert result.content == "here you go"


async def test_invoke_surfaces_structured_content_when_no_text_blocks():
    # A spec-conformant tool can return its payload only in structuredContent
    # with empty content blocks; it must not read as an empty success.
    session = StubSession(
        {
            "lookup": SimpleNamespace(
                content=[], isError=False, structuredContent={"temp_c": 21}
            )
        }
    )
    adapter = seeded_adapter({"home": session}, [spec("home.lookup")])
    result = await adapter.invoke("home.lookup", {})
    assert result.ok is True
    assert result.content == {"temp_c": 21}


async def test_invoke_maps_is_error_to_failed_result():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("cal.delete_event", {"id": "x"})
    assert result.ok is False
    assert result.error == "no such event"


async def test_invoke_session_fault_becomes_failed_result():
    adapter, _, _ = make_adapter()
    result = await adapter.invoke("cal.broken", {})
    assert result.ok is False
    assert result.error is not None and "pipe closed" in result.error


async def test_invoke_unknown_name_raises_not_found():
    adapter, _, _ = make_adapter()
    with pytest.raises(ToolNotFoundError):
        await adapter.invoke("cal.nope", {})
    with pytest.raises(ToolNotFoundError):
        await adapter.invoke("unnamespaced", {})
    with pytest.raises(ToolNotFoundError):
        await adapter.invoke("ghost.list_events", {})


# ---------------------------------------------------------------------------
# Liveness: dead-marking, lazy reconnect, cooldown
# ---------------------------------------------------------------------------


async def test_transport_failure_marks_server_dead_and_hides_its_specs():
    dying = StubSession({"list_events": ClosedResourceError("stream torn down")})
    healthy = StubSession({"send": call_result(text_block("sent"))})
    connector = FakeConnector()  # every reconnect attempt fails
    clock = FakeClock(1000.0)
    adapter = seeded_adapter(
        {"cal": dying, "mail": healthy},
        [spec("cal.list_events"), spec("mail.send")],
        connector=connector,
        monotonic=clock,
    )

    result = await adapter.invoke("cal.list_events", {})

    # Design choice: transport death yields ToolResult(ok=False), never
    # ToolNotFoundError. The dispatcher resolved the spec while the server
    # was alive, so the agent must see a reportable failure, not be told
    # the tool it was offered does not exist.
    assert result.ok is False
    assert result.error is not None and "lost" in result.error
    assert adapter._states["cal"].session is None

    # The dead server's specs vanish; the listing pass makes one (failing)
    # revival attempt before giving up on it for this cooldown window.
    names = [s.name for s in await adapter.list_tools()]
    assert names == ["mail.send"]
    assert connector.calls == 1

    # Inside the cooldown neither listing nor invoking retries the connect.
    clock.now += 1.0
    names = [s.name for s in await adapter.list_tools()]
    assert names == ["mail.send"]
    followup = await adapter.invoke("cal.list_events", {})
    assert followup.ok is False
    assert followup.error is not None and "unavailable" in followup.error
    assert connector.calls == 1

    # The healthy sibling is untouched throughout.
    ok = await adapter.invoke("mail.send", {})
    assert ok.ok is True


async def test_call_timeout_marks_server_dead():
    stalled = StubSession({"slow": TimeoutError()})
    adapter = seeded_adapter(
        {"cal": stalled}, [spec("cal.slow")], monotonic=FakeClock(0.0)
    )

    result = await adapter.invoke("cal.slow", {})

    # A timed-out call is indistinguishable from a wedged process, so the
    # session is abandoned rather than burning the full timeout again.
    assert result.ok is False
    assert result.error is not None and "timed out" in result.error
    assert adapter._states["cal"].session is None


async def test_is_error_result_keeps_server_live_and_advertised():
    session = StubSession(
        {"delete_event": call_result(text_block("no such event"), is_error=True)}
    )
    adapter = seeded_adapter(
        {"cal": session}, [spec("cal.delete_event")], monotonic=FakeClock(0.0)
    )

    result = await adapter.invoke("cal.delete_event", {"id": "x"})

    # isError is the tool complaining, not the transport dying: the server
    # stays live and its tools stay on offer.
    assert result.ok is False
    assert adapter._states["cal"].session is session
    names = [s.name for s in await adapter.list_tools()]
    assert names == ["cal.delete_event"]


async def test_ordinary_tool_exception_keeps_server_live():
    adapter, cal, _ = make_adapter()
    await adapter.invoke("cal.broken", {})
    assert adapter._states["cal"].session is cal
    names = [s.name for s in await adapter.list_tools()]
    assert "cal.broken" in names


async def test_reconnect_after_cooldown_is_single_flight():
    dying = StubSession({"list_events": ClosedResourceError()})
    revived = StubSession({"list_events": call_result(text_block("fresh"))})
    connector = FakeConnector([(revived, [mcp_tool("list_events", "List")])])
    clock = FakeClock(0.0)
    adapter = seeded_adapter(
        {"cal": dying}, [spec("cal.list_events")], connector=connector, monotonic=clock
    )
    # As if connect() happened at t=0: the cooldown counts from the last
    # connect attempt, not from the moment of death.
    adapter._states["cal"].last_attempt = 0.0

    clock.now = 10.0
    dead = await adapter.invoke("cal.list_events", {})
    assert dead.ok is False

    # Still inside the cooldown window: no reconnect attempt is made.
    clock.now = 30.0
    gated = await adapter.invoke("cal.list_events", {})
    assert gated.ok is False
    assert connector.calls == 0

    # Past the cooldown, several concurrent invokes trigger exactly one
    # connect: the per-server lock serialises them and last_attempt is
    # stamped before the attempt, so the losers reuse the fresh session.
    clock.now = 61.0
    results = await asyncio.gather(
        *(adapter.invoke("cal.list_events", {}) for _ in range(5))
    )
    assert connector.calls == 1
    assert adapter._states["cal"].session is revived
    assert all(r.ok is True and r.content == "fresh" for r in results)


async def test_failed_reconnect_is_attempted_once_per_cooldown_window():
    dying = StubSession({"list_events": ClosedResourceError()})
    connector = FakeConnector()  # raises ConnectionError on every attempt
    clock = FakeClock(0.0)
    adapter = seeded_adapter(
        {"cal": dying}, [spec("cal.list_events")], connector=connector, monotonic=clock
    )
    adapter._states["cal"].last_attempt = 0.0

    clock.now = 100.0
    await adapter.invoke("cal.list_events", {})  # transport death

    results = await asyncio.gather(
        *(adapter.invoke("cal.list_events", {}) for _ in range(4))
    )
    assert connector.calls == 1
    assert all(r.ok is False for r in results)

    # The next window buys exactly one more attempt, no matter how many
    # calls and listings arrive in between.
    clock.now = 130.0
    assert await adapter.list_tools() == []
    assert connector.calls == 1
    clock.now = 161.0
    retry = await adapter.invoke("cal.list_events", {})
    assert retry.ok is False
    assert connector.calls == 2


async def test_reconnect_refreshes_specs_from_the_restarted_server():
    dying = StubSession({"old_tool": ClosedResourceError()})
    revived = StubSession({"new_tool": call_result(text_block("hello"))})
    connector = FakeConnector([(revived, [mcp_tool("new_tool", "New")])])
    clock = FakeClock(0.0)
    adapter = seeded_adapter(
        {"cal": dying}, [spec("cal.old_tool")], connector=connector, monotonic=clock
    )

    await adapter.invoke("cal.old_tool", {})  # transport death

    # The restarted server is re-listed; the stale cache must not resurrect
    # tools the new process no longer offers.
    clock.now = 61.0
    names = [s.name for s in await adapter.list_tools()]
    assert names == ["cal.new_tool"]
    assert connector.calls == 1

    result = await adapter.invoke("cal.new_tool", {})
    assert result.ok is True
    assert result.content == "hello"
    with pytest.raises(ToolNotFoundError):
        await adapter.invoke("cal.old_tool", {})


# ---------------------------------------------------------------------------
# connect() and close(): per-server stacks
# ---------------------------------------------------------------------------


async def test_connect_skips_failing_server_and_releases_its_stack(monkeypatch):
    entered: list[str] = []
    closed: list[str] = []

    @contextlib.asynccontextmanager
    async def tracked(tag: str):
        entered.append(tag)
        try:
            yield
        finally:
            closed.append(tag)

    good_session = StubSession({"send": call_result(text_block("sent"))})

    async def fake_connector(self, config, stack):
        await stack.enter_async_context(tracked(config.name))
        if config.name == "bad":
            raise TimeoutError("initialize timed out")
        return good_session, [mcp_tool("send", "Send mail")]

    monkeypatch.setattr(McpToolAdapter, "_mcp_connector", fake_connector)
    adapter = await McpToolAdapter.connect([server_config("bad"), server_config("mail")])

    # The failing server's own stack was unwound at failure time: no zombie
    # subprocess context survives until shutdown. The sibling is unharmed.
    assert entered == ["bad", "mail"]
    assert closed == ["bad"]
    assert adapter._states["bad"].session is None

    names = [s.name for s in await adapter.list_tools()]
    assert names == ["mail.send"]
    result = await adapter.invoke("mail.send", {})
    assert result.ok is True

    await adapter.close()
    assert sorted(closed) == ["bad", "mail"]


async def test_close_releases_every_servers_contexts():
    closed: list[str] = []

    @contextlib.asynccontextmanager
    async def resource(tag: str):
        try:
            yield
        finally:
            closed.append(tag)

    adapter = seeded_adapter(
        {"cal": StubSession(), "mail": StubSession()},
        [spec("cal.a"), spec("mail.b")],
    )
    await adapter._states["cal"].stack.enter_async_context(resource("cal"))
    await adapter._states["mail"].stack.enter_async_context(resource("mail"))

    await adapter.close()

    assert sorted(closed) == ["cal", "mail"]


async def test_close_after_dead_mark_does_not_raise():
    dying = StubSession({"list_events": ClosedResourceError()})
    healthy = StubSession({"send": call_result(text_block("sent"))})
    adapter = seeded_adapter(
        {"cal": dying, "mail": healthy},
        [spec("cal.list_events"), spec("mail.send")],
        monotonic=FakeClock(0.0),
    )

    closed: list[str] = []

    @contextlib.asynccontextmanager
    async def resource(tag: str):
        try:
            yield
        finally:
            closed.append(tag)

    def explode() -> None:
        # Mimics anyio complaining about cancel scopes exiting in a
        # different task when an abandoned session is torn down.
        raise RuntimeError("cancel scope exited in a different task")

    adapter._states["cal"].stack.callback(explode)
    await adapter._states["mail"].stack.enter_async_context(resource("mail"))
    adapter._states["mail"].stack.callback(explode)

    dead = await adapter.invoke("cal.list_events", {})
    assert dead.ok is False
    assert adapter._states["cal"].session is None

    # close() swallows per-server teardown faults and still releases what it
    # can: mail's resource exits even though its stack also exploded.
    await adapter.close()
    assert closed == ["mail"]


# ---------------------------------------------------------------------------
# CompositeToolAdapter
# ---------------------------------------------------------------------------


def make_sources() -> tuple[FakeTools, FakeTools]:
    first = FakeTools()
    first.add("alpha", handler=lambda **kw: "first-alpha")
    first.add("shared", handler=lambda **kw: "first-shared")
    second = FakeTools()
    second.add("shared", handler=lambda **kw: "second-shared")
    second.add("beta", handler=lambda **kw: "second-beta")
    return first, second


async def test_composite_list_tools_concatenates_in_source_order():
    first, second = make_sources()
    composite = CompositeToolAdapter([first, second])
    names = [s.name for s in await composite.list_tools()]
    assert names == ["alpha", "shared", "shared", "beta"]


async def test_composite_routes_to_owning_source():
    first, second = make_sources()
    composite = CompositeToolAdapter([first, second])

    result = await composite.invoke("beta", {"x": 1})

    assert result.ok is True
    assert result.content == "second-beta"
    assert first.invocations == []
    assert second.invocations == [("beta", {"x": 1})]


async def test_composite_collision_first_source_wins():
    first, second = make_sources()
    composite = CompositeToolAdapter([first, second])

    result = await composite.invoke("shared", {})

    assert result.content == "first-shared"
    assert first.invocations == [("shared", {})]
    assert second.invocations == []


async def test_composite_unknown_name_raises_not_found():
    first, second = make_sources()
    composite = CompositeToolAdapter([first, second])
    with pytest.raises(ToolNotFoundError):
        await composite.invoke("gamma", {})


async def test_composite_over_mcp_adapter_routes_namespaced_names():
    mcp_session = StubSession({"send": call_result(text_block("sent"))})
    mcp_adapter = seeded_adapter(
        {"mail": mcp_session},
        [spec("mail.send", CapabilityTier.REVERSIBLE_WRITE)],
    )
    local = FakeTools()
    local.add("current_time")
    composite = CompositeToolAdapter([local, mcp_adapter])

    names = [s.name for s in await composite.list_tools()]
    assert names == ["current_time", "mail.send"]

    result = await composite.invoke("mail.send", {"to": "owner"})
    assert result.ok is True
    assert result.content == "sent"
