"""The single composition root: every adapter meets every port here.

build_system wires the whole brain from one AlfredConfig. Real mode uses
sqlite and Ollama; fake mode swaps in the in-memory store and a DryRunModel
so the entire pipeline runs offline. build_model and build_transports are
the factories the CLI uses for the construction it cannot get from
build_system (the model probe, and transports that need the core's handler
first). Nothing outside this module constructs an adapter or a domain
service; the CLI only orchestrates what these factories return.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alfred.adapters.discord_transport import DiscordTransportAdapter
from alfred.adapters.http_transport import HttpTransportAdapter
from alfred.adapters.local_tools import LocalToolAdapter
from alfred.adapters.mcp_tools import CompositeToolAdapter, McpToolAdapter
from alfred.adapters.ollama_model import OllamaModelAdapter
from alfred.adapters.openai_model import OpenAiModelAdapter
from alfred.adapters.sqlite_store import SqliteStoreAdapter
from alfred.adapters.telegram_transport import TelegramTransportAdapter
from alfred.config import AlfredConfig
from alfred.domain.builder import AgentBuilder
from alfred.domain.conductor import Conductor
from alfred.domain.dispatch import ToolDispatcher
from alfred.domain.executor import AgentExecutor
from alfred.domain.governance import PendingActions, Policy, Proposals, WorkflowTrust
from alfred.domain.lifecycle import LapseDoctor
from alfred.domain.memory import MemoryService
from alfred.domain.reflection import ReflectionEngine
from alfred.domain.registry import AgentRegistry
from alfred.domain.roadmap import RoadmapPlanner, RoadmapService, WinsLedger
from alfred.domain.schemas import InboundMessage
from alfred.domain.user_model import UserModelService
from alfred.errors import AlfredError, ToolNotFoundError
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

    def has_route(self, prefix: str) -> bool:
        # Forwarded so the core's route check sees through the wrapper.
        # Non-Multi inners (chat mode, tests) default to True, keeping the
        # configured-channel preference exactly as before.
        inner_has_route = getattr(self.inner, "has_route", None)
        if inner_has_route is None:
            return True
        return bool(inner_has_route(prefix))


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

    def has_route(self, prefix: str) -> bool:
        return prefix in self._routes or self._default is not None

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


_DRY_RUN_REPLY: dict[str, Any] = {
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

# A real (if generic) path so `chat --fake` demonstrates the headline
# small-wins flow end to end offline: goal -> next win -> win -> advance. The
# planner overwrites the goal with the owner's actual one and makes the first
# milestone active.
_DRY_RUN_ROADMAP = {
    "goal": "set by the planner",
    "real_lever": "dry-run lever (offline mode)",
    "milestones": [
        {
            "title": "Name the one small first step",
            "why": "a path starts the moment a doable step is named",
            "done_signal": "the first step is written down",
            "anchor": "right after reading this",
        },
        {
            "title": "Do that step once",
            "why": "a single rep proves it is small enough to not fail",
            "done_signal": "it is done, however small",
            "anchor": "after the first step is named",
        },
        {
            "title": "Repeat it tomorrow",
            "why": "a second rep is where momentum actually begins",
            "done_signal": "done two days running",
            "anchor": "the same cue, the next day",
        },
    ],
}


def _zero_value(prop: Mapping[str, Any], defs: Mapping[str, Any]) -> Any:
    """Type-appropriate zero value for one schema property, top level only."""
    if "$ref" in prop:
        prop = defs.get(str(prop["$ref"]).rsplit("/", 1)[-1], {})
    if prop.get("enum"):
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
            case "Roadmap":
                return json.dumps(_DRY_RUN_ROADMAP)
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
    roadmap: RoadmapService
    core: AlfredCore
    heartbeat: Heartbeat
    transport: TransportPort
    mcp_slot: McpSlot | None = None
    # (folder_name, reason) for agent folders that failed to load; surfaced
    # in 'agents', 'status', and doctor so version skew is never invisible.
    skipped_agents: list[tuple[str, str]] = field(default_factory=list)


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
        model = build_model(config)

    memory = MemoryService(store, clock)
    mcp_slot = McpSlot()
    tools = CompositeToolAdapter(
        [LocalToolAdapter(store, clock, memory=memory), mcp_slot]
    )

    if transport is None:
        transport = SwitchableTransport()

    skipped_agents: list[tuple[str, str]] = []
    registry = load_agents(config.agents_dir, skipped=skipped_agents)
    user_model = UserModelService(store, clock)

    def folders_on_disk() -> set[str]:
        # The builder names new agents against what is actually on disk,
        # not just what loaded: a broken folder still owns its slug, and
        # materialisation would refuse to overwrite it after approval.
        root = Path(config.agents_dir)
        if not root.is_dir():
            return set()
        return {child.name for child in root.iterdir() if child.is_dir()}

    policy = Policy(
        auto_approve_reversible=config.policy.auto_approve_reversible,
        dry_run_cross_system=config.policy.dry_run_cross_system,
    )
    pending = PendingActions(
        store, clock, ttl_hours=config.policy.pending_action_ttl_hours
    )
    proposals = Proposals(store, clock)
    trust = WorkflowTrust(
        store, clock, threshold=config.policy.trust_after_approvals
    )
    dispatcher = ToolDispatcher(tools, store, clock, policy, pending, trust=trust)
    executor = AgentExecutor(
        model, tools, dispatcher, user_model, store, clock, memory=memory
    )
    conductor = Conductor(model, clock)
    builder = AgentBuilder(
        model, user_model, store, clock, taken_names=folders_on_disk
    )
    reflection = ReflectionEngine(model, user_model, store, clock)
    lapse_doctor = LapseDoctor(model, clock)
    roadmap = RoadmapService(
        RoadmapPlanner(model, clock), WinsLedger(store, clock), store, clock
    )

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
        roadmap,
        store,
        clock,
        transport,
        config,
        config.agents_dir,
        memory=memory,
        skipped_agents=skipped_agents,
        trust=trust,
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
        roadmap=roadmap,
        core=core,
        heartbeat=heartbeat,
        transport=transport,
        mcp_slot=mcp_slot,
        skipped_agents=skipped_agents,
    )


async def connect_mcp(system: ComposedSystem) -> None:
    """Connect configured MCP servers into the composite's placeholder slot.

    Separate from build_system because MCP connection is async; the CLI
    awaits this in run mode. A no-op when nothing is configured.
    """
    if not system.config.mcp_servers or system.mcp_slot is None:
        return
    system.mcp_slot.inner = await McpToolAdapter.connect(system.config.mcp_servers)


async def probe_mcp(config: AlfredConfig) -> McpToolAdapter:
    """A throwaway connected adapter for doctor's MCP check.

    Lives here so the CLI never constructs an adapter itself; the caller
    reads statuses() and MUST close() it.
    """
    return await McpToolAdapter.connect(config.mcp_servers)


def build_model(config: AlfredConfig) -> OllamaModelAdapter | OpenAiModelAdapter:
    """The real model adapter for the configured provider.

    Used by build_system in real mode and by the CLI's probe and demo
    paths, so nothing outside this module ever picks an adapter. Both
    concrete types expose ensure_model() for the probe.
    """
    if config.llm.provider == "openai":
        return OpenAiModelAdapter(config.llm)
    return OllamaModelAdapter(config.llm)


@dataclass
class TransportSetup:
    """What build_transports decided: the wired transports plus CLI notes.

    routes maps a channel prefix ("discord"/"telegram"/"http") to its
    transport. notes are (level, message) lines for the CLI to render;
    level is one of "step", "warn", "bad". Keeping the env-gating and
    construction here means the CLI only renders and runs what it is given.
    """

    routes: dict[str, TransportPort] = field(default_factory=dict)
    notes: list[tuple[str, str]] = field(default_factory=list)


def build_transports(
    config: AlfredConfig,
    handler: Callable[[InboundMessage], Awaitable[None]],
) -> TransportSetup:
    """Construct every transport whose credentials are present.

    The owner reaches the same brain from whichever channel is in reach. The
    cyclic dependency (transports need the core's handler, the core needs a
    transport) is broken by the SwitchableTransport slot build_system leaves;
    the CLI swaps these routes into it after wiring.
    """
    setup = TransportSetup()

    if os.environ.get(config.discord.token_env):
        if config.discord.owner_id == 0:
            setup.notes.append(
                (
                    "warn",
                    "discord.owner_id is 0 (the default), so EVERY Discord "
                    "message will be ignored. Set your user id in config/alfred.yaml.",
                )
            )
        setup.routes["discord"] = DiscordTransportAdapter(config.discord, handler)
        setup.notes.append(("step", "Discord gateway starting"))
    else:
        setup.notes.append(("step", f"Discord off ({config.discord.token_env} not set)"))
        if config.discord.channel_id:
            setup.notes.append(
                (
                    "warn",
                    "discord.channel_id is set but the Discord transport is "
                    f"off ({config.discord.token_env} not set); scheduled "
                    "messages fall back to wherever you last spoke. Clear "
                    "discord.channel_id or set the token.",
                )
            )

    if config.telegram.enabled and os.environ.get(config.telegram.token_env):
        if config.telegram.owner_id == 0:
            setup.notes.append(
                (
                    "warn",
                    "telegram.owner_id is 0, so EVERY Telegram message will be "
                    "ignored. Set your user id in config/alfred.yaml.",
                )
            )
        setup.routes["telegram"] = TelegramTransportAdapter(config.telegram, handler)
        setup.notes.append(("step", "Telegram polling starting"))
    elif config.telegram.enabled:
        setup.notes.append(
            ("step", f"Telegram off ({config.telegram.token_env} not set)")
        )

    if config.http.enabled:
        try:
            setup.routes["http"] = HttpTransportAdapter(config.http, handler)
        except AlfredError as exc:
            setup.notes.append(("bad", f"HTTP API off: {exc}"))
        else:
            setup.notes.append(
                ("step", f"HTTP API on {config.http.host}:{config.http.port}")
            )

    return setup
