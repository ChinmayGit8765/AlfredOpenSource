"""The alfred command line: init, chat, run, agents list, demo-roundtrip.

The only module allowed to print. Everything heavier than argument
parsing and terminal I/O is delegated to the composition root, so each
subcommand reads as: load config, build system, drive it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from alfred.adapters.discord_transport import DiscordTransportAdapter
from alfred.adapters.ollama_model import OllamaModelAdapter
from alfred.config import AlfredConfig, load_config
from alfred.domain.schemas import InboundMessage, Plan
from alfred.domain.structured import structured_call
from alfred.errors import AlfredError
from alfred.logging_setup import configure_logging
from alfred.ports import ModelPort, OutboundMessage
from alfred.runtime.agent_loader import load_agents
from alfred.runtime.composition import (
    ComposedSystem,
    DryRunModel,
    SwitchableTransport,
    build_system,
    connect_mcp,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("config/alfred.yaml")
_OLLAMA_PROBE_TIMEOUT = 8.0
# PowerShell pipes prepend a UTF-8 BOM to the first stdin line; depending
# on the console codepage it decodes as U+FEFF or as three mojibake chars.
_BOM_MARKS = (chr(0xFEFF), chr(0xEF) + chr(0xBB) + chr(0xBF))

_OLLAMA_HINT = (
    "ALFRED needs a local model. Install Ollama (https://ollama.com), start "
    "it, and pull a model: ollama pull qwen3:8b. Or try things offline with "
    "--fake where supported."
)

_DEFAULT_CONFIG = """\
# ALFRED configuration. See config/alfred.example.yaml for every option
# with documentation; only the essentials are listed here.

# Where ALFRED keeps its database and runtime state.
data_dir: data

# Directory scanned at startup for agent folders (manifest.yaml + agent.md).
agents_dir: agents

# Local model backend. Pull the model first: ollama pull qwen3:8b
llm:
  host: "http://127.0.0.1:11434"
  name: "qwen3:8b"

# Discord transport. Set owner_id to YOUR Discord user id; the bot token
# is read from the ALFRED_DISCORD_TOKEN environment variable, never from
# this file.
discord:
  owner_id: 0
  channel_id: null
"""


class LoopbackTransport:
    """TransportPort for the chat REPL: replies print straight to the terminal."""

    async def send(self, message: OutboundMessage) -> None:
        print(f"ALFRED> {message.text}")


async def _probe_ollama(config: AlfredConfig) -> str:
    """Return a usable model name or raise; bounded so init never hangs."""
    adapter = OllamaModelAdapter(config.llm)
    return await asyncio.wait_for(adapter.ensure_model(), timeout=_OLLAMA_PROBE_TIMEOUT)


async def _close_quietly(obj: object) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.exception("error while closing %s", type(obj).__name__)


async def _shutdown(system: ComposedSystem) -> None:
    if system.mcp_slot is not None and system.mcp_slot.inner is not None:
        await _close_quietly(system.mcp_slot.inner)
    await _close_quietly(system.store)


# --- subcommands -------------------------------------------------------------


async def _cmd_init(config_path: str | None) -> int:
    target = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    if target.exists():
        print(f"Config already exists, refusing to overwrite: {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        print(f"Wrote {target}")

    config = load_config(target)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    print(f"Data directory ready: {config.data_dir}")

    try:
        chosen = await _probe_ollama(config)
    except Exception as exc:
        print(
            "Note: Ollama was not reachable "
            f"({type(exc).__name__}). Chat and run will need it; start the "
            "Ollama server and pull a model (ollama pull qwen3:8b), or use "
            "chat --fake to try ALFRED offline."
        )
    else:
        print(f"Ollama is reachable; ALFRED will use model: {chosen}")
    return 0


async def _cmd_chat(fake: bool, config_path: str | None) -> int:
    config = load_config(config_path)
    system = build_system(config, fake=fake, transport=LoopbackTransport())
    names = ", ".join(a.manifest.name for a in system.registry.active()) or "none"
    mode = " (dry-run mode)" if fake else ""
    print(f"ALFRED chat{mode}. Active agents: {names}. Empty line or 'exit' quits.")
    try:
        while True:
            try:
                line = await asyncio.to_thread(input, "you> ")
            except EOFError:
                break
            # Piped input on Windows can carry a UTF-8 BOM; strip it.
            for mark in _BOM_MARKS:
                line = line.removeprefix(mark)
            line = line.strip()
            if not line or line.lower() in ("exit", "quit"):
                break
            await system.core.handle_inbound(InboundMessage(channel="cli", text=line))
            if system.core.stop_requested:
                break
    except KeyboardInterrupt:
        pass
    finally:
        print("Goodbye.")
        await _shutdown(system)
    return 0


async def _cmd_run(config_path: str | None) -> int:
    config = load_config(config_path)
    system = build_system(config)
    try:
        chosen = await _probe_ollama(config)
    except Exception as exc:
        print(f"Cannot start: no usable model ({exc}).")
        print(_OLLAMA_HINT)
        await _shutdown(system)
        return 1
    # ensure_model may have picked a fallback; make the adapter use it.
    config.llm.name = chosen
    print(f"Model ready: {chosen}")

    await connect_mcp(system)

    discord = DiscordTransportAdapter(config.discord, system.core.handle_inbound)
    if isinstance(system.transport, SwitchableTransport):
        system.transport.inner = discord

    tasks = [
        asyncio.create_task(discord.start(), name="discord"),
        asyncio.create_task(system.heartbeat.run_forever(), name="heartbeat"),
    ]
    print("ALFRED is running. Ctrl-C to stop.")
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_quietly(discord)
        await _shutdown(system)
        print("Stopped.")
    return 0


async def _cmd_agents_list(config_path: str | None) -> int:
    config = load_config(config_path)
    registry = load_agents(config.agents_dir)
    agents = registry.all()
    if not agents:
        print(f"No agents found in {config.agents_dir}.")
        return 0
    rows = [("name", "lifecycle", "domain", "schedule", "tools")]
    for agent in agents:
        manifest = agent.manifest
        rows.append(
            (
                manifest.name,
                manifest.lifecycle.value,
                manifest.domain or "-",
                manifest.schedule.kind,
                str(len(manifest.allowed_tools)),
            )
        )
    widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    return 0


async def _cmd_demo_roundtrip(fake: bool, config_path: str | None) -> int:
    config = load_config(config_path)
    model: ModelPort
    if fake:
        model = DryRunModel()
    else:
        try:
            config.llm.name = await _probe_ollama(config)
        except Exception as exc:
            print(f"Cannot run the round-trip: {exc}")
            print(_OLLAMA_HINT)
            return 1
        model = OllamaModelAdapter(config.llm)

    plan = await structured_call(
        model,
        schema=Plan,
        system="You produce structured training plans.",
        user="Produce a tiny 3-item example training plan for next week.",
    )
    print(json.dumps(plan.model_dump(mode="json"), indent=2))
    print("validated: Plan")
    return 0


# --- entry point ------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfred",
        description="ALFRED: a self-hosted, local-first life-optimization system.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create config/alfred.yaml and the data dir")
    p_init.add_argument("--config", default=None, help="config file path to create")

    p_chat = sub.add_parser("chat", help="talk to ALFRED in a terminal REPL")
    p_chat.add_argument("--fake", action="store_true", help="offline dry-run mode")
    p_chat.add_argument("--config", default=None, help="config file path")

    p_run = sub.add_parser("run", help="full service: Discord plus heartbeat")
    p_run.add_argument("--config", default=None, help="config file path")

    p_agents = sub.add_parser("agents", help="agent folder utilities")
    agents_sub = p_agents.add_subparsers(dest="agents_command", required=True)
    p_list = agents_sub.add_parser("list", help="list discovered agents")
    p_list.add_argument("--config", default=None, help="config file path")

    p_demo = sub.add_parser(
        "demo-roundtrip",
        help="phase-1 proof: one validated structured model call",
    )
    p_demo.add_argument("--fake", action="store_true", help="use the dry-run model")
    p_demo.add_argument("--config", default=None, help="config file path")

    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    match args.command:
        case "init":
            return await _cmd_init(args.config)
        case "chat":
            return await _cmd_chat(args.fake, args.config)
        case "run":
            return await _cmd_run(args.config)
        case "agents":
            return await _cmd_agents_list(args.config)
        case "demo-roundtrip":
            return await _cmd_demo_roundtrip(args.fake, args.config)
        case _:  # argparse enforces choices; this is unreachable
            return 2


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        code = asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("\nInterrupted; goodbye.")
        code = 0
    except AlfredError as exc:
        print(f"Error: {exc}")
        code = 1
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
