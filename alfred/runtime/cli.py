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
import os
import queue
import threading
from pathlib import Path

from rich.panel import Panel
from rich.syntax import Syntax

import alfred
from alfred.config import AlfredConfig, load_config
from alfred.domain.schemas import InboundMessage, Plan
from alfred.domain.structured import structured_call
from alfred.errors import AlfredError, ConfigError
from alfred.logging_setup import configure_logging
from alfred.ports import ModelPort, OutboundMessage
from alfred.runtime import ui
from alfred.runtime.agent_loader import load_agents
from alfred.runtime.composition import (
    ComposedSystem,
    DryRunModel,
    MultiTransport,
    SwitchableTransport,
    build_model,
    build_system,
    build_transports,
    connect_mcp,
)
from alfred.runtime.ui import console

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


def _model_hint(config: AlfredConfig) -> str:
    """The right unblock hint for the configured model provider."""
    if config.llm.provider == "openai":
        return (
            f"ALFRED could not use the API endpoint at {config.llm.host}. "
            f"Check llm.host and llm.name in the config, and that "
            f"{config.llm.api_key_env} is exported if the provider needs a "
            "key. Or try things offline with --fake where supported."
        )
    return _OLLAMA_HINT

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
    """TransportPort for the chat REPL: replies render straight to the terminal."""

    async def send(self, message: OutboundMessage) -> None:
        ui.print_reply(message.text)


class _StdinReader:
    """Prompted line reads on a daemon thread.

    A plain asyncio.to_thread(input, ...) leaves a non-daemon executor
    thread blocked in input() at shutdown, so Ctrl-C appears to hang until
    the user presses Enter. A daemon thread never holds the process open.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._prompts: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True, name="alfred-stdin").start()

    def _pump(self) -> None:
        while True:
            prompt = self._prompts.get()
            try:
                line = input(prompt)
            except EOFError:
                self._loop.call_soon_threadsafe(self._lines.put_nowait, None)
                return
            self._loop.call_soon_threadsafe(self._lines.put_nowait, line)

    async def read(self, prompt: str) -> str | None:
        """Return the next line, or None on EOF."""
        self._prompts.put(prompt)
        return await self._lines.get()


async def _probe_model(config: AlfredConfig) -> str:
    """Return a usable model name or raise; bounded so init never hangs."""
    adapter = build_model(config)
    try:
        return await asyncio.wait_for(
            adapter.ensure_model(), timeout=_OLLAMA_PROBE_TIMEOUT
        )
    finally:
        # The probe adapter is throwaway; close it so an httpx client
        # never leaks. Ollama's adapter has no close and is skipped.
        await _close_quietly(adapter)


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
    await _close_quietly(system.model)
    await _close_quietly(system.store)


# --- subcommands -------------------------------------------------------------


async def _cmd_init(config_path: str | None) -> int:
    ui.print_banner("init")
    target = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    if target.exists():
        console.print(ui.check_line("warn", "config", f"already exists, kept: {target}"))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        console.print(ui.check_line("ok", "config", f"wrote {target}"))

    config = load_config(target)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    console.print(ui.check_line("ok", "data dir", str(config.data_dir)))

    try:
        chosen = await _probe_model(config)
    except Exception as exc:
        hint = (
            "Start Ollama and pull a model (ollama pull qwen3:8b), or try "
            "chat --fake offline."
            if config.llm.provider == "ollama"
            else f"Check llm.host and {config.llm.api_key_env}, or try "
            "chat --fake offline."
        )
        console.print(
            ui.check_line(
                "warn",
                "model",
                f"not reachable ({type(exc).__name__}); chat and run need it. {hint}",
            )
        )
    else:
        console.print(ui.check_line("ok", "model", f"reachable, model: {chosen}"))
    console.print(
        "\n[chrome]Next:[/] alfred chat --fake [chrome](offline demo) or[/] "
        "alfred doctor [chrome](full readiness check)[/]"
    )
    return 0


async def _cmd_chat(fake: bool, config_path: str | None) -> int:
    config = load_config(config_path)
    system = build_system(config, fake=fake, transport=LoopbackTransport())
    if not fake:
        # Probe before the banner, exactly like run: the composed adapter
        # reads config.llm.name at call time, so resolving a fallback here
        # is what makes the README's "fallbacks are tried automatically"
        # true in chat too. Without it, a missing primary model fails
        # every single message with a generic notice.
        try:
            config.llm.name = await _probe_model(config)
        except Exception as exc:
            console.print(f"[bad]Cannot start chat:[/] no usable model ({exc}).")
            console.print(f"[chrome]{_model_hint(config)}[/]")
            await _shutdown(system)
            return 1
        # Chat and run expose the same tool surface for the same agents.
        await connect_mcp(system)
    ui.print_banner(f"chat {ui.DOT} dry-run" if fake else f"chat {ui.DOT} {config.llm.name}")
    names = ", ".join(a.manifest.name for a in system.registry.active()) or "none"
    console.print(f"[chrome]Active agents:[/] {names}")
    console.print("[chrome]Empty line or 'exit' quits; 'help' lists commands.[/]\n")
    reader = _StdinReader()
    prompt = ui.prompt_string()
    try:
        while True:
            line = await reader.read(prompt)
            if line is None:
                break
            # Piped input on Windows can carry a UTF-8 BOM; strip it.
            for mark in _BOM_MARKS:
                line = line.removeprefix(mark)
            line = line.strip()
            if not line or line.lower() in ("exit", "quit"):
                break
            with console.status("[chrome]ALFRED is thinking...[/]", spinner="dots"):
                await system.core.handle_inbound(
                    InboundMessage(channel="cli", text=line)
                )
            if system.core.stop_requested:
                break
    except KeyboardInterrupt:
        pass
    finally:
        console.print("[chrome]Goodbye.[/]")
        await _shutdown(system)
    return 0


async def _cmd_run(config_path: str | None) -> int:
    config = load_config(config_path)
    system = build_system(config)
    ui.print_banner("full service")
    try:
        chosen = await _probe_model(config)
    except Exception as exc:
        console.print(f"[bad]Cannot start:[/] no usable model ({exc}).")
        console.print(f"[chrome]{_model_hint(config)}[/]")
        await _shutdown(system)
        return 1
    # ensure_model may have picked a fallback; make the adapter use it.
    config.llm.name = chosen
    ui.step(f"model ready: {chosen}")

    await connect_mcp(system)
    if config.mcp_servers:
        ui.step(f"MCP servers configured: {len(config.mcp_servers)}")

    # Every transport with credentials runs; the owner reaches the same
    # brain from whichever channel is in reach. Construction lives in the
    # composition root; here we only render its notes and run what it built.
    setup = build_transports(config, system.core.handle_inbound)
    for level, text in setup.notes:
        if level == "warn":
            console.print(f"[warn]WARNING:[/] {text}")
        elif level == "bad":
            console.print(f"[warn]{text}[/]")
        else:
            ui.step(text)

    routes = setup.routes
    adapters: list[object] = list(routes.values())
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(adapter.start(), name=prefix)  # type: ignore[attr-defined]
        for prefix, adapter in routes.items()
    ]

    if not tasks:
        console.print(
            "[bad]No transport is configured.[/] Set a Discord or Telegram "
            "token, or enable the HTTP API, then run again. Meanwhile, "
            "alfred chat works in this terminal."
        )
        await _shutdown(system)
        return 1

    if isinstance(system.transport, SwitchableTransport):
        system.transport.inner = MultiTransport(routes)

    async def watch_stop() -> None:
        # The owner's kill switch: "alfred stop" in chat sets the flag and
        # this watcher brings the whole service down.
        while not system.core.stop_requested:
            await asyncio.sleep(1)

    tasks.append(asyncio.create_task(watch_stop(), name="stop-watch"))
    if config.heartbeat.enabled:
        tasks.append(
            asyncio.create_task(system.heartbeat.run_forever(), name="heartbeat")
        )
        ui.step("heartbeat running")
    else:
        console.print(
            "[warn]Heartbeat disabled in config:[/] ALFRED will only react, "
            "never initiate."
        )

    console.print(
        "\n[ok]ALFRED is running.[/] [chrome]Ctrl-C (or 'alfred stop' in chat) "
        "to stop.[/]"
    )
    exit_code = 0
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is None:
                continue
            name = type(exc).__name__
            if name == "LoginFailure":
                console.print(
                    "[bad]Discord rejected the bot token.[/] Check the "
                    f"{config.discord.token_env} environment variable."
                )
            else:
                console.print(
                    f"[bad]Service task {task.get_name()!r} failed[/] "
                    f"({name}): {exc}"
                )
            logger.error("service task %s failed: %s", task.get_name(), exc)
            exit_code = 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in adapters:
            await _close_quietly(adapter)
        await _shutdown(system)
        console.print("[chrome]Stopped.[/]")
    return exit_code


async def _cmd_agents_list(config_path: str | None) -> int:
    config = load_config(config_path)
    skipped: list[tuple[str, str]] = []
    registry = load_agents(config.agents_dir, skipped=skipped)
    agents = registry.all()
    if not agents and not skipped:
        console.print(f"[warn]No agents found in {config.agents_dir}.[/]")
        return 0
    if agents:
        console.print(ui.agents_table(agents))
    for name, reason in skipped:
        console.print(f"[warn]NOT LOADED:[/] {name}: {reason}")
    return 0


async def _cmd_demo_roundtrip(fake: bool, config_path: str | None) -> int:
    config = load_config(config_path)
    model: ModelPort
    if fake:
        model = DryRunModel()
    else:
        try:
            config.llm.name = await _probe_model(config)
        except Exception as exc:
            console.print(f"[bad]Cannot run the round-trip:[/] {exc}")
            console.print(f"[chrome]{_model_hint(config)}[/]")
            return 1
        model = build_model(config)

    with console.status("[chrome]one structured call, validated...[/]", spinner="dots"):
        plan = await structured_call(
            model,
            schema=Plan,
            system="You produce structured training plans.",
            user="Produce a tiny 3-item example training plan for next week.",
        )
    rendered = Syntax(
        json.dumps(plan.model_dump(mode="json"), indent=2),
        "json",
        background_color="default",
    )
    console.print(
        Panel(
            rendered,
            title="model output, schema-validated",
            title_align="left",
            border_style="accent",
        )
    )
    console.print(f"[ok]{'✓' if ui.FANCY else '+'}[/] validated: Plan")
    return 0


async def _cmd_doctor(config_path: str | None) -> int:
    """Readiness check: everything ALFRED needs, one glance, no surprises."""
    ui.print_banner("doctor")
    hard_fail = False

    target = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.print(ui.check_line("bad", "config", str(exc)))
        return 1
    if target.is_file():
        console.print(ui.check_line("ok", "config", str(target)))
    else:
        console.print(
            ui.check_line("warn", "config", "no config file; using defaults (alfred init)")
        )

    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        probe = config.data_dir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        console.print(ui.check_line("ok", "data dir", f"writable: {config.data_dir}"))
    except OSError as exc:
        console.print(ui.check_line("bad", "data dir", f"not writable: {exc}"))
        hard_fail = True

    skipped: list[tuple[str, str]] = []
    registry = load_agents(config.agents_dir, skipped=skipped)
    agents = registry.all()
    if agents and not skipped:
        names = ", ".join(a.manifest.name for a in agents)
        console.print(ui.check_line("ok", "agents", f"{len(agents)} loaded: {names}"))
    elif agents:
        # A partially loaded fleet is a warning, never an ok: version skew
        # silently unloading an agent is exactly what doctor exists to catch.
        console.print(
            ui.check_line(
                "warn",
                "agents",
                f"{len(agents)} loaded, {len(skipped)} NOT loaded: "
                + "; ".join(f"{name} ({reason})" for name, reason in skipped),
            )
        )
    else:
        console.print(
            ui.check_line("warn", "agents", f"none found in {config.agents_dir}")
        )

    try:
        chosen = await _probe_model(config)
        console.print(
            ui.check_line(
                "ok",
                "model",
                f"{config.llm.provider} {ui.DOT} {config.llm.host} "
                f"{ui.DOT} model {chosen}",
            )
        )
    except Exception as exc:
        console.print(
            ui.check_line(
                "warn",
                "model",
                f"unreachable ({type(exc).__name__}); chat --fake still works",
            )
        )
    if config.llm.provider == "openai":
        if os.environ.get(config.llm.api_key_env):
            console.print(
                ui.check_line("ok", "llm api key", f"{config.llm.api_key_env} is set")
            )
        else:
            console.print(
                ui.check_line(
                    "warn",
                    "llm api key",
                    f"{config.llm.api_key_env} not set; only keyless "
                    "endpoints will work",
                )
            )

    if config.discord.owner_id == 0:
        console.print(
            ui.check_line("warn", "discord owner", "owner_id is 0; every message ignored")
        )
    else:
        console.print(ui.check_line("ok", "discord owner", str(config.discord.owner_id)))
    if os.environ.get(config.discord.token_env):
        console.print(
            ui.check_line("ok", "discord token", f"{config.discord.token_env} is set")
        )
    else:
        console.print(
            ui.check_line(
                "warn",
                "discord token",
                f"{config.discord.token_env} not set; Discord transport stays off",
            )
        )

    if not config.telegram.enabled:
        console.print(ui.check_line("off", "telegram", "disabled in config"))
    elif not os.environ.get(config.telegram.token_env):
        console.print(
            ui.check_line(
                "warn", "telegram", f"enabled but {config.telegram.token_env} not set"
            )
        )
    elif config.telegram.owner_id == 0:
        console.print(
            ui.check_line("warn", "telegram", "owner_id is 0; every message ignored")
        )
    else:
        console.print(
            ui.check_line("ok", "telegram", f"owner {config.telegram.owner_id}")
        )

    if not config.http.enabled:
        console.print(ui.check_line("off", "http api", "disabled in config"))
    elif not os.environ.get(config.http.token_env):
        console.print(
            ui.check_line(
                "warn",
                "http api",
                f"enabled but {config.http.token_env} not set; it will not start",
            )
        )
    else:
        console.print(
            ui.check_line(
                "ok", "http api", f"{config.http.host}:{config.http.port}, token set"
            )
        )

    if config.mcp_servers:
        try:
            import mcp  # noqa: F401

            console.print(
                ui.check_line(
                    "ok", "mcp", f"{len(config.mcp_servers)} server(s) configured"
                )
            )
        except ImportError:
            console.print(
                ui.check_line(
                    "bad",
                    "mcp",
                    'servers configured but the extra is missing: uv pip install "alfred[mcp]"',
                )
            )
    else:
        console.print(ui.check_line("off", "mcp", "no servers configured (phase 6)"))

    if hard_fail:
        console.print("\n[bad]Not ready.[/] Fix the failures above.")
        return 1
    console.print("\n[ok]Ready.[/] [chrome]alfred chat to talk, alfred run for the full service.[/]")
    return 0


# --- entry point ------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfred",
        description="ALFRED: a self-hosted, local-first life-optimization system.",
    )
    parser.add_argument(
        "--version", action="version", version=f"alfred {alfred.__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create config/alfred.yaml and the data dir")
    p_init.add_argument("--config", default=None, help="config file path to create")

    p_doctor = sub.add_parser("doctor", help="readiness check: config, model, agents, transport")
    p_doctor.add_argument("--config", default=None, help="config file path")

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
        case "doctor":
            return await _cmd_doctor(args.config)
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
    args = _build_parser().parse_args(argv)
    # The long-running service logs its pulse; interactive commands stay
    # quiet so the conversation is the only thing on screen.
    configure_logging(logging.INFO if args.command == "run" else logging.WARNING)
    try:
        code = asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        console.print("\n[chrome]Interrupted; goodbye.[/]")
        code = 0
    except AlfredError as exc:
        console.print(f"[bad]Error:[/] {exc}")
        code = 1
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
