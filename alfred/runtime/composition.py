"""The single composition root: every adapter meets every port here.

build_system wires the whole brain from one AlfredConfig. Real mode uses
sqlite and Ollama; fake mode swaps in the in-memory store and a DryRunModel
so the entire pipeline runs offline. Nothing outside this module (and the
CLI that drives it) constructs adapters or domain services.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alfred.adapters.local_tools import LocalToolAdapter
from alfred.adapters.mcp_tools import CompositeToolAdapter, McpToolAdapter
from alfred.adapters.ollama_model import OllamaModelAdapter
from alfred.adapters.sqlite_store import SqliteStoreAdapter
from alfred.config import AlfredConfig
from alfred.domain.builder import AgentBuilder
from alfred.domain.conductor import Conductor
from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.executor import AgentExecutor
from alfred.domain.governance import PendingActions, Policy, Proposals
from alfred.domain.lifecycle import LapseDoctor
from alfred.domain.memory import MemoryService
from alfred.domain.reflection import ReflectionEngine
from alfred.domain.registry import AgentRegistry
from alfred.domain.user_model import UserModelService
from alfred.errors import ToolNotFoundError
from alfred.ports import (
    ModelMessage,
    ModelOptions,
    ModelPort,
    OutboundMessage,
    StorePort,
    ToolPort,
    ToolResult,
    ToolSpec,
    TransportPort,
)
from alfred.runtime.agent_loader import load_agents
from alfred.runtime.core import AlfredCore
from alfred.runtime.heartbeat import Heartbeat
from alfred.testing.fakes import CapturingTransport, MemoryStore

logger = logging.getLogger(__name__)


class SystemClock:
    """ClockPort over the real wall clock, always timezone-aware.

    An explicit IANA timezone name pins schedules and quiet hours to that
    zone; otherwise the system local zone applies.
    """

    def __init__(self, tz_name: str | None = None) -> None:
        self._tz: ZoneInfo | None = None
        if tz_name:
            try:
                self._tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                logger.warning(
                    "unknown timezone %r; using the system local zone", tz_name
                )

    def now(self) -> datetime:
        if self._tz is not None:
            return datetime.now(self._tz)
        return datetime.now().astimezone()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SwitchableTransport:
    """TransportPort whose delivery target can be swapped after wiring.

    The core needs a transport at construction, but the real transports
    need the core's handler first. This wrapper breaks the cycle: build
    with a capturing default, switch the inner later.
    """

    def __init__(self, inner: TransportPort | None = None) -> None:
        self.inner: TransportPort = inner if inner is not None else CapturingTransport()

    async def send(self, message: OutboundMessage) -> None:
        await self.inner.send(message)


class MultiTransport:
    """Routes outbound messages to the right transport by channel namespace.

    Channels carry their transport as a prefix ("discord:123",
    "telegram:456", "http:abc"); the message goes to whichever registered
    transport owns that prefix. Bare numeric channels route to the
    default (Discord, historically unprefixed). Unroutable messages are
    dropped loudly, never silently.
    """

    def __init__(
        self,
        routes: dict[str, TransportPort],
        default: TransportPort | None = None,
    ) -> None:
        self._routes = dict(routes)
        self._default = default

    async def send(self, message: OutboundMessage) -> None:
        prefix = message.channel.split(":", 1)[0] if ":" in message.channel else ""
        transport = self._routes.get(prefix)
        if transport is None and message.channel.isdigit():
            transport = self._routes.get("discord", self._default)
        if transport is None:
            transport = self._default
        if transport is None:
            logger.warning(
                "no transport for channel %s; dropping: %.120s",
                message.channel,
                message.text,
            )
            return
        await transport.send(message)


class McpSlot:
    """Empty ToolPort seat in the composite, filled by connect_mcp later.

    build_system stays synchronous, but McpToolAdapter.connect is async,
    so the composite is built with this placeholder and the CLI awaits
    connect_mcp(system) in run mode to populate it.
    """

    def __init__(self) -> None:
        self.inner: ToolPort | None = None

    async def list_tools(self) -> list[ToolSpec]:
        if self.inner is None:
            return []
        return await self.inner.list_tools()

    async def invoke(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        if self.inner is None:
            raise ToolNotFoundError(f"unknown tool: {name}")
        return await self.inner.invoke(name, args)


_DRY_RUN_REPLY = {
    "reply": (
        "This is dry-run mode: no local model is connected, so I cannot "
        "produce real plans or use tools. Commands, routing, and governance "
        "are all live; start Ollama and run without --fake for the real thing."
    ),
    "plan": None,
    "tool_calls": [],
    "observations": [],
    "done": True,
}

# Schemas the zero-filler cannot satisfy (nested required models, slug
# patterns) or where zero values would wedge a flow (elicitation that never
# becomes satisfied) get canned shapes so the fake pipeline runs end to end.
_DRY_RUN_BLUEPRINT = {
    "manifest": {
        "name": "dry-run-agent",
        "description": (
            "Placeholder agent designed in dry-run mode; rebuild with a "
            "real model connected before relying on it."
        ),
        "shape": "habit",
        "lifecycle": "proposed",
        "schedule": {"kind": "daily", "time": "08:00"},
        "capacity_cost": 1,
    },
    "prompt_md": (
        "# dry-run-agent\n\n"
        "Identity: a placeholder built offline in dry-run mode.\n"
        "Scope: demonstrates the build flow; it optimises nothing real.\n"
        "Smallest viable size: the smallest possible, by construction.\n"
        "Anchor: after opening the terminal, glance at this agent.\n"
        "Tone: a lapse is data, never a moral failure; no streak pressure, "
        "no fake urgency.\n"
        "Output: one short check-in line."
    ),
}

_DRY_RUN_ELICIT = {
    "question": "",
    "satisfied": True,
    "real_lever": "dry-run lever (offline mode; no real elicitation happened)",
}


def _zero_value(prop: Mapping[str, Any], defs: Mapping[str, Any]) -> Any:
    """Type-appropriate zero value for one schema property, top level only."""
    if "$ref" in prop:
        prop = defs.get(str(prop["$ref"]).rsplit("/", 1)[-1], {})
    if "enum" in prop and prop["enum"]:
        return prop["enum"][0]
    if "anyOf" in prop:
        options = prop["anyOf"]
        if any(option.get("type") == "null" for option in options):
            return None
        return _zero_value(options[0], defs) if options else None
    match prop.get("type"):
        case "string":
            return "dry-run"
        case "array":
            return []
        case "integer":
            return 0
        case "number":
            return 0
        case "boolean":
            return True
        case _:
            return {}


class DryRunModel:
    """ModelPort fake for offline mode: canned, schema-shaped JSON only.

    Good enough to exercise the whole pipeline without an LLM; it never
    pretends to think. AgentReply gets a friendly hardcoded shape; any
    other schema gets its required fields filled with zero values.
    """

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        json_schema: Mapping[str, Any] | None = None,
        options: ModelOptions | None = None,
    ) -> str:
        if json_schema is None:
            return "dry-run"
        match json_schema.get("title"):
            case "AgentReply":
                return json.dumps(_DRY_RUN_REPLY)
            case "AgentBlueprint":
                return json.dumps(_DRY_RUN_BLUEPRINT)
            case "_ElicitStep":
                return json.dumps(_DRY_RUN_ELICIT)
        defs = json_schema.get("$defs", {})
        properties = json_schema.get("properties", {})
        filled = {
            name: _zero_value(properties.get(name, {}), defs)
            for name in json_schema.get("required", [])
        }
        return json.dumps(filled)


@dataclass
class ComposedSystem:
    """Everything the CLI needs to drive ALFRED, wired and ready."""

    config: AlfredConfig
    store: StorePort
    model: ModelPort
    tools: ToolPort
    registry: AgentRegistry
    user_model: UserModelService
    memory: MemoryService
    dispatcher: ToolDispatcher
    pending: PendingActions
    proposals: Proposals
    executor: AgentExecutor
    conductor: Conductor
    builder: AgentBuilder
    reflection: ReflectionEngine
    lapse_doctor: LapseDoctor
    core: AlfredCore
    heartbeat: Heartbeat
    transport: TransportPort
    mcp_slot: McpSlot | None = None


def build_system(
    config: AlfredConfig,
    *,
    fake: bool = False,
    transport: TransportPort | None = None,
) -> ComposedSystem:
    """Wire the full system from config. The only composition root."""
    clock = SystemClock(config.timezone)

    store: StorePort
    model: ModelPort
    if fake:
        store = MemoryStore()
        model = DryRunModel()
    else:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteStoreAdapter(config.db_path)
        model = OllamaModelAdapter(config.llm)

    memory = MemoryService(store, clock)
    mcp_slot = McpSlot()
    tools = CompositeToolAdapter(
        [LocalToolAdapter(store, clock, memory=memory), mcp_slot]
    )

    if transport is None:
        transport = SwitchableTransport()

    registry = load_agents(config.agents_dir)
    user_model = UserModelService(store, clock)
    policy = Policy(auto_approve_reversible=config.policy.auto_approve_reversible)
    pending = PendingActions(
        store, clock, ttl_hours=config.policy.pending_action_ttl_hours
    )
    proposals = Proposals(store, clock)
    dispatcher = ToolDispatcher(tools, store, clock, policy, pending)
    executor = AgentExecutor(
        model, tools, dispatcher, user_model, store, clock, memory=memory
    )
    conductor = Conductor(model, clock)
    builder = AgentBuilder(model, user_model, store, clock)
    reflection = ReflectionEngine(model, user_model, store, clock)
    lapse_doctor = LapseDoctor(model, clock)

    core = AlfredCore(
        registry,
        executor,
        conductor,
        builder,
        user_model,
        dispatcher,
        pending,
        proposals,
        reflection,
        lapse_doctor,
        store,
        clock,
        transport,
        config,
        config.agents_dir,
        memory=memory,
    )
    heartbeat = Heartbeat(
        registry=registry,
        clock=clock,
        store=store,
        runner=core.run_scheduled,
        config=config.heartbeat,
    )

    logger.info(
        "system composed: fake=%s agents=%d mcp_servers=%d",
        fake,
        len(registry.all()),
        len(config.mcp_servers),
    )
    return ComposedSystem(
        config=config,
        store=store,
        model=model,
        tools=tools,
        registry=registry,
        user_model=user_model,
        memory=memory,
        dispatcher=dispatcher,
        pending=pending,
        proposals=proposals,
        executor=executor,
        conductor=conductor,
        builder=builder,
        reflection=reflection,
        lapse_doctor=lapse_doctor,
        core=core,
        heartbeat=heartbeat,
        transport=transport,
        mcp_slot=mcp_slot,
    )


async def connect_mcp(system: ComposedSystem) -> None:
    """Connect configured MCP servers into the composite's placeholder slot.

    Separate from build_system because MCP connection is async; the CLI
    awaits this in run mode. A no-op when nothing is configured.
    """
    if not system.config.mcp_servers or system.mcp_slot is None:
        return
    system.mcp_slot.inner = await McpToolAdapter.connect(system.config.mcp_servers)
