"""MCP tool adapters: the action layer behind an optional dependency.

McpToolAdapter turns every connected MCP server into ToolPort capabilities,
namespaced "<server>.<tool>" so two servers can expose same-named tools
without collision. Tiers come from the owner's per-server classification;
anything unclassified is DESTRUCTIVE because an unknown capability gets the
strictest gate, never a free pass. CompositeToolAdapter stitches multiple
tool sources (local plus MCP) into one ToolPort for the dispatcher.

The mcp package is optional, so it is imported lazily inside connect();
importing this module must always succeed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from alfred.config import McpServerConfig
from alfred.errors import ConfigError, ToolNotFoundError
from alfred.ports.tools import CapabilityTier, ToolPort, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "MCP support requires the optional mcp dependency; "
    'install with: uv pip install "alfred[mcp]"'
)

# A hung MCP server must never block startup, an agent run, or the
# heartbeat indefinitely: every session operation gets a deadline.
CONNECT_TIMEOUT_S = 20.0
CALL_TIMEOUT_S = 60.0


def _tier_for(config: McpServerConfig, tool: str, qualified: str) -> CapabilityTier:
    """Tier from the owner's classification; strictest tier when absent or bad."""
    raw = config.tool_tiers.get(tool, config.tool_tiers.get(qualified))
    if raw is None:
        return CapabilityTier.DESTRUCTIVE
    try:
        return CapabilityTier(raw)
    except ValueError:
        logger.warning(
            "invalid tier %r configured for MCP tool %s; defaulting to destructive",
            raw,
            qualified,
        )
        return CapabilityTier.DESTRUCTIVE


class McpToolAdapter:
    """ToolPort over one or more MCP servers."""

    def __init__(
        self,
        sessions: Mapping[str, Any] | None = None,
        specs: list[ToolSpec] | None = None,
        exit_stack: contextlib.AsyncExitStack | None = None,
    ) -> None:
        # Takes prebuilt state so connect() stays thin and tests can exercise
        # routing and mapping without spawning server processes.
        self._sessions: dict[str, Any] = dict(sessions or {})
        self._specs: list[ToolSpec] = list(specs or [])
        self._exit_stack = exit_stack or contextlib.AsyncExitStack()

    @classmethod
    async def connect(cls, servers: list[McpServerConfig]) -> McpToolAdapter:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise ConfigError(_INSTALL_HINT) from exc

        stack = contextlib.AsyncExitStack()
        sessions: dict[str, Any] = {}
        specs: list[ToolSpec] = []
        for server in servers:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
                    read, write = await stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=server.command,
                                args=server.args,
                                env=server.env or None,
                            )
                        )
                    )
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    tools = await cls._list_all_tools(session)
            except Exception as exc:
                # A broken or hanging connector must not take ALFRED down:
                # skip it, keep the rest.
                logger.warning(
                    "MCP server '%s' failed to start, skipping: %s", server.name, exc
                )
                continue
            sessions[server.name] = session
            specs.extend(cls._specs_for_server(server, tools))
            logger.info(
                "connected MCP server '%s' with %d tools", server.name, len(tools)
            )
        return cls(sessions=sessions, specs=specs, exit_stack=stack)

    @staticmethod
    async def _list_all_tools(session: Any) -> list[Any]:
        """Follow list_tools pagination; one page is the common case."""
        tools: list[Any] = []
        cursor: str | None = None
        while True:
            listed = await session.list_tools(cursor)
            tools.extend(listed.tools)
            cursor = getattr(listed, "nextCursor", None)
            if not cursor:
                return tools

    @staticmethod
    def _specs_for_server(config: McpServerConfig, tools: Sequence[Any]) -> list[ToolSpec]:
        """Map MCP tool definitions for one server into namespaced ToolSpecs."""
        specs: list[ToolSpec] = []
        for tool in tools:
            qualified = f"{config.name}.{tool.name}"
            specs.append(
                ToolSpec(
                    name=qualified,
                    description=tool.description or "",
                    parameters=getattr(tool, "inputSchema", None) or {},
                    tier=_tier_for(config, tool.name, qualified),
                    source=f"mcp:{config.name}",
                )
            )
        return specs

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def invoke(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        if not any(spec.name == name for spec in self._specs):
            raise ToolNotFoundError(f"unknown MCP tool: {name}")
        server, _, tool = name.partition(".")
        session = self._sessions.get(server)
        if session is None:
            raise ToolNotFoundError(f"no connected MCP server for tool: {name}")
        try:
            async with asyncio.timeout(CALL_TIMEOUT_S):
                result = await session.call_tool(tool, dict(args))
        except TimeoutError:
            logger.warning("MCP tool %s timed out after %.0fs", name, CALL_TIMEOUT_S)
            return ToolResult(
                ok=False, error=f"{name} timed out after {CALL_TIMEOUT_S:.0f}s"
            )
        except Exception as exc:
            logger.warning("MCP tool %s failed: %s", name, exc)
            return ToolResult(ok=False, error=f"{name} failed: {exc}")
        parts: list[str] = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            elif hasattr(block, "model_dump_json"):
                # Non-text content (images, resources) is surfaced as its
                # JSON form rather than silently dropped.
                parts.append(block.model_dump_json())
        text = "\n".join(parts)
        if result.isError:
            return ToolResult(
                ok=False,
                content=text or None,
                error=text or f"{name} reported an error",
            )
        return ToolResult(ok=True, content=text)

    async def close(self) -> None:
        await self._exit_stack.aclose()


class CompositeToolAdapter:
    """One ToolPort over many sources; the first source claiming a name wins."""

    def __init__(self, sources: list[ToolPort]) -> None:
        self._sources = list(sources)

    async def list_tools(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for source in self._sources:
            specs.extend(await source.list_tools())
        return specs

    async def invoke(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        # Resolved per call: tool lists can change as connectors come and go.
        for source in self._sources:
            if any(spec.name == name for spec in await source.list_tools()):
                return await source.invoke(name, args)
        raise ToolNotFoundError(f"unknown tool: {name}")
