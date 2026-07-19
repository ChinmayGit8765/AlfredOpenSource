"""MCP tool adapters: the action layer behind an optional dependency.

McpToolAdapter turns every connected MCP server into ToolPort capabilities,
namespaced "<server>.<tool>" so two servers can expose same-named tools
without collision. Tiers come from the owner's per-server classification;
anything unclassified is DESTRUCTIVE because an unknown capability gets the
strictest gate, never a free pass. CompositeToolAdapter stitches multiple
tool sources (local plus MCP) into one ToolPort for the dispatcher.

Every server owns its own AsyncExitStack so one dying server never unwinds
a sibling's contexts, and a session killed by a transport failure is
reconnected lazily (per-server lock plus cooldown) instead of staying dead
until process restart.

The mcp package is optional, so it is imported lazily inside connect paths;
importing this module must always succeed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
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
# Dead servers retry lazily and at most once per window, so concurrent agent
# runs cannot stampede subprocess spawns and a permanently broken server
# costs one connect attempt per window rather than one per call.
RECONNECT_COOLDOWN_S = 60.0

# anyio only arrives transitively with the optional mcp extra, so transport
# failures are recognised by exception name rather than by importing it.
_TRANSPORT_EXC_NAMES = frozenset(
    {"ClosedResourceError", "BrokenResourceError", "EndOfStream"}
)

# Injectable session opener: enters transport contexts on the given stack and
# returns (session, raw tool definitions). Tests swap it to drive death and
# revival without spawning subprocesses.
Connector = Callable[
    [McpServerConfig, contextlib.AsyncExitStack],
    Awaitable[tuple[Any, list[Any]]],
]


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


def _is_transport_failure(exc: BaseException) -> bool:
    """True when the session itself is gone, not when one tool call failed.

    Only these kill a session; ordinary tool exceptions and isError results
    must never take a healthy server offline.
    """
    if isinstance(exc, ConnectionError):
        return True
    for klass in type(exc).__mro__:
        if klass.__name__ in _TRANSPORT_EXC_NAMES:
            return True
    # mcp surfaces a dropped transport as McpError("Connection closed");
    # other McpErrors are per-call failures and leave the session alone.
    if type(exc).__name__ == "McpError" and "connection closed" in str(exc).lower():
        return True
    return False


class _ServerState:
    """Connection state for one configured server.

    The stack is private to the server so tearing one down never unwinds a
    sibling's contexts; the lock and last_attempt throttle reconnects.
    """

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.stack = contextlib.AsyncExitStack()
        self.session: Any | None = None
        self.specs: list[ToolSpec] = []
        # Monotonic seconds of the most recent connect attempt; None means
        # never attempted, so the first use may connect immediately.
        self.last_attempt: float | None = None
        self.lock = asyncio.Lock()


@dataclass(frozen=True)
class McpServerStatus:
    """One configured server's state, for doctor and status surfaces.

    unclassified holds the bare tool names missing from tool_tiers: they
    still work, on the strictest gate, but the owner deserves to see the
    list because "every call asks first" usually means a forgotten entry.
    """

    name: str
    connected: bool
    specs: tuple[ToolSpec, ...]
    unclassified: tuple[str, ...]


class McpToolAdapter:
    """ToolPort over one or more MCP servers."""

    def __init__(
        self,
        servers: Sequence[McpServerConfig] | None = None,
        *,
        connector: Connector | None = None,
        monotonic: Callable[[], float] | None = None,
        reconnect_cooldown_s: float = RECONNECT_COOLDOWN_S,
    ) -> None:
        # The connector and clock are injectable so tests can exercise death
        # and reconnection without subprocesses; production wiring never
        # overrides them.
        self._states: dict[str, _ServerState] = {
            server.name: _ServerState(server) for server in (servers or [])
        }
        self._connector: Connector = (
            connector if connector is not None else self._mcp_connector
        )
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._cooldown_s = reconnect_cooldown_s

    @classmethod
    async def connect(cls, servers: list[McpServerConfig]) -> McpToolAdapter:
        # Probe the optional dep up front so a missing install fails loudly
        # with the hint instead of silently skipping every server below.
        try:
            import mcp  # noqa: F401
        except ImportError as exc:
            raise ConfigError(_INSTALL_HINT) from exc
        adapter = cls(servers=servers)
        for state in adapter._states.values():
            state.last_attempt = adapter._monotonic()
            try:
                await adapter._connect_server(state)
            except Exception as exc:
                # A broken or hanging connector must not take ALFRED down:
                # skip it, keep the rest. Lazy reconnect gives it another
                # chance once the cooldown passes.
                logger.warning(
                    "MCP server '%s' failed to start, skipping: %s",
                    state.config.name,
                    exc,
                )
                continue
            logger.info(
                "connected MCP server '%s' with %d tools",
                state.config.name,
                len(state.specs),
            )
        return adapter

    async def _mcp_connector(
        self, config: McpServerConfig, stack: contextlib.AsyncExitStack
    ) -> tuple[Any, list[Any]]:
        """The production connector: spawn the server and open a session."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise ConfigError(_INSTALL_HINT) from exc

        read, write = await stack.enter_async_context(
            stdio_client(
                StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=config.env or None,
                )
            )
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools = await self._list_all_tools(session)
        return session, tools

    async def _connect_server(self, state: _ServerState) -> None:
        """Open a fresh session on a fresh stack; never leave a half-open one."""
        stack = contextlib.AsyncExitStack()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                session, tools = await self._connector(state.config, stack)
        except BaseException:
            # Unwound here, in the same task and except frame that entered
            # the contexts, so anyio cancel scopes exit cleanly and a failed
            # initialize cannot leak a zombie subprocess onto a live stack.
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise
        state.stack = stack
        state.session = session
        # Fresh listing, never the stale cache: a restarted server may
        # expose a different tool set than it did before it died.
        state.specs = self._specs_for_server(state.config, tools)

    async def _ensure_session(self, state: _ServerState) -> Any | None:
        """Return a live session, reconnecting lazily when the last one died."""
        if state.session is not None:
            return state.session
        async with state.lock:
            if state.session is not None:
                # Another task reconnected while we waited on the lock.
                return state.session
            now = self._monotonic()
            if (
                state.last_attempt is not None
                and now - state.last_attempt < self._cooldown_s
            ):
                return None
            # Stamped before the attempt so a slow failure still holds the
            # cooldown and queued callers cannot pile on more attempts.
            state.last_attempt = now
            try:
                await self._connect_server(state)
            except Exception as exc:
                logger.warning(
                    "MCP server '%s' reconnect failed: %s", state.config.name, exc
                )
                return None
            logger.info(
                "reconnected MCP server '%s' with %d tools",
                state.config.name,
                len(state.specs),
            )
            return state.session

    async def _mark_dead(self, state: _ServerState, session: Any) -> None:
        """Abandon a session whose transport failed so the next use reconnects."""
        if state.session is not session:
            # A concurrent reconnect already replaced this session; do not
            # tear down the healthy successor.
            return
        state.session = None
        stack, state.stack = state.stack, contextlib.AsyncExitStack()
        # Best effort: the session is being abandoned anyway, and anyio may
        # raise RuntimeError when cancel scopes close from a different task.
        with contextlib.suppress(Exception):
            await stack.aclose()

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

    def statuses(self) -> list[McpServerStatus]:
        """Current per-server state, without triggering reconnects.

        A snapshot for doctor and status surfaces: connect() ran just
        before, so poking a dead server here would only double the wait.
        """
        out: list[McpServerStatus] = []
        for state in self._states.values():
            tiers = state.config.tool_tiers
            unclassified = tuple(
                bare
                for spec in state.specs
                # Mirrors _tier_for: either the bare or the qualified name
                # counts as classified.
                if (bare := spec.name.partition(".")[2]) not in tiers
                and spec.name not in tiers
            )
            out.append(
                McpServerStatus(
                    name=state.config.name,
                    connected=state.session is not None,
                    specs=tuple(state.specs),
                    unclassified=unclassified,
                )
            )
        return out

    async def list_tools(self) -> list[ToolSpec]:
        # Dead servers are not advertised: offering a tool that cannot run
        # burns an agent round on a guaranteed failure. Each listing gives a
        # dead server one cooldown-gated chance to come back.
        specs: list[ToolSpec] = []
        for state in self._states.values():
            if state.session is None and await self._ensure_session(state) is None:
                continue
            specs.extend(state.specs)
        return specs

    async def invoke(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        server, _, tool = name.partition(".")
        state = self._states.get(server)
        if state is None:
            raise ToolNotFoundError(f"unknown MCP tool: {name}")
        session = await self._ensure_session(state)
        if session is None:
            # Down and staying down for now (failed reconnect or cooldown).
            # A failed result, not an exception: the dispatch that reached
            # here resolved the spec while the server was alive, so the
            # agent should see a reportable failure, not a caller error.
            return ToolResult(
                ok=False,
                error=(
                    f"MCP server '{server}' is unavailable; "
                    f"{name} cannot run right now"
                ),
            )
        if not any(spec.name == name for spec in state.specs):
            raise ToolNotFoundError(f"unknown MCP tool: {name}")
        try:
            async with asyncio.timeout(CALL_TIMEOUT_S):
                result = await session.call_tool(tool, dict(args))
        except TimeoutError:
            # A timeout is indistinguishable from a wedged server process:
            # abandon the session so the next call reconnects instead of
            # burning another full timeout on a corpse.
            await self._mark_dead(state, session)
            logger.warning("MCP tool %s timed out after %.0fs", name, CALL_TIMEOUT_S)
            return ToolResult(
                ok=False, error=f"{name} timed out after {CALL_TIMEOUT_S:.0f}s"
            )
        except Exception as exc:
            if _is_transport_failure(exc):
                await self._mark_dead(state, session)
                logger.warning(
                    "MCP server '%s' connection lost during %s: %s", server, name, exc
                )
                return ToolResult(
                    ok=False,
                    error=f"{name} failed: connection to '{server}' lost: {exc}",
                )
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
        # Some tools return their payload only in structuredContent with no
        # content blocks; surface it rather than reporting an empty success.
        structured = getattr(result, "structuredContent", None)
        if not text and structured is not None:
            return ToolResult(ok=True, content=structured)
        return ToolResult(ok=True, content=text)

    async def close(self) -> None:
        for state in self._states.values():
            state.session = None
            # Best effort per server: one refusing to shut down must not
            # keep the others' subprocesses alive.
            with contextlib.suppress(Exception):
                await state.stack.aclose()


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
