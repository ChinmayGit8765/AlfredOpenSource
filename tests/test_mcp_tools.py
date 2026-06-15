"""Tests for alfred.adapters.mcp_tools: MCP and composite tool adapters.

No subprocesses: the adapter is constructed directly with stubbed session
objects carrying the same shape connect() would have built.
"""

from __future__ import annotations

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
    adapter = McpToolAdapter(sessions={"cal": cal, "mail": mail}, specs=specs)
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
    adapter = McpToolAdapter(sessions={"cam": session}, specs=[spec("cam.shot")])
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
    adapter = McpToolAdapter(sessions={"home": session}, specs=[spec("home.lookup")])
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


async def test_close_releases_held_contexts():
    closed: list[str] = []

    @contextlib.asynccontextmanager
    async def resource():
        try:
            yield
        finally:
            closed.append("done")

    stack = contextlib.AsyncExitStack()
    await stack.enter_async_context(resource())
    adapter = McpToolAdapter(sessions={}, specs=[], exit_stack=stack)

    await adapter.close()

    assert closed == ["done"]


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
    mcp_adapter = McpToolAdapter(
        sessions={"mail": mcp_session},
        specs=[spec("mail.send", CapabilityTier.REVERSIBLE_WRITE)],
    )
    local = FakeTools()
    local.add("current_time")
    composite = CompositeToolAdapter([local, mcp_adapter])

    names = [s.name for s in await composite.list_tools()]
    assert names == ["current_time", "mail.send"]

    result = await composite.invoke("mail.send", {"to": "owner"})
    assert result.ok is True
    assert result.content == "sent"
