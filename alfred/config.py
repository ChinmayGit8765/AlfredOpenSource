"""ALFRED configuration.

A single pydantic-validated config object, loaded from a YAML file with
secrets pulled from the environment. Credentials never live in config
files and are never logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from alfred.errors import ConfigError

# Every config model forbids unknown keys: a typo like
# `auto_aprove_reversible` must fail loudly at load time, never be silently
# dropped while the owner believes they changed a safety default.
_STRICT = ConfigDict(extra="forbid")


class ModelConfig(BaseModel):
    """Local model backend settings."""

    model_config = _STRICT

    host: str = "http://127.0.0.1:11434"  # localhost by default; sovereignty
    name: str = "qwen3:8b"
    fallbacks: list[str] = Field(default_factory=lambda: ["qwen2.5:7b", "llama3.1:8b"])
    temperature: float = 0.4


class DiscordConfig(BaseModel):
    """Discord transport settings. The token comes from the environment."""

    model_config = _STRICT

    token_env: str = "ALFRED_DISCORD_TOKEN"
    owner_id: int = 0  # Discord user id; only this user is ever obeyed
    channel_id: int | None = None  # optional: restrict to one channel

    def token(self) -> str:
        value = os.environ.get(self.token_env, "")
        if not value:
            raise ConfigError(
                f"Discord token not found in environment variable {self.token_env}"
            )
        return value


class TelegramConfig(BaseModel):
    """Telegram transport settings. The bot token comes from the environment."""

    model_config = _STRICT

    enabled: bool = False
    token_env: str = "ALFRED_TELEGRAM_TOKEN"
    owner_id: int = 0  # Telegram user id; only this user is ever obeyed

    def token(self) -> str:
        value = os.environ.get(self.token_env, "")
        if not value:
            raise ConfigError(
                f"Telegram token not found in environment variable {self.token_env}"
            )
        return value


class HttpConfig(BaseModel):
    """Local HTTP API: lets Shortcuts, Tasker, curl, anything reach ALFRED.

    Off by default and bound to localhost by default; exposure beyond the
    machine is a deliberate opt-in, and the bearer token is mandatory.
    """

    model_config = _STRICT

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "ALFRED_HTTP_TOKEN"

    def token(self) -> str:
        value = os.environ.get(self.token_env, "")
        if not value:
            raise ConfigError(
                f"HTTP API token not found in environment variable {self.token_env}; "
                "the API never runs without one"
            )
        return value


class HeartbeatConfig(BaseModel):
    """Proactive scheduler settings."""

    model_config = _STRICT

    enabled: bool = True
    tick_seconds: int = Field(default=60, ge=5)
    quiet_hours: str = "22:30-07:30"  # "HH:MM-HH:MM" local; no proactive pings
    reflection_days: int = 7  # cadence of periodic Conductor reflection
    # Cadence of the gentle "your next small win" nudge for the active roadmap.
    # 0 disables it; the nudge only ever sends when a next win actually exists,
    # and never during quiet hours. A surface, never a streak or a nag.
    roadmap_nudge_days: int = Field(default=1, ge=0)
    # Age in days after which the message and audit LOGS are pruned by a
    # daily sweep. 0 (the default) keeps everything forever: deleting audit
    # history is an owner decision, never a silent adapter default. Pending
    # actions, proposals, plans, and memories are never swept regardless.
    retention_days: int = Field(default=0, ge=0)


class PolicyConfig(BaseModel):
    """Governance knobs. Defaults are the safe direction."""

    model_config = _STRICT

    auto_approve_reversible: bool = True  # reversible writes run, but are audited
    dry_run_cross_system: bool = True  # multi-system workflows preview first
    pending_action_ttl_hours: int = 24


class McpServerConfig(BaseModel):
    """One MCP server ALFRED may connect to (phase 6, the action layer)."""

    model_config = _STRICT

    # No dots: tools are namespaced "<name>.<tool>" and routed by splitting on
    # the first dot, so a dotted server name would misroute every call. The
    # slug also keeps two servers from sharing a namespace.
    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Unlisted tools default to DESTRUCTIVE; classify to relax, never to bypass.
    tool_tiers: dict[str, str] = Field(default_factory=dict)


class AlfredConfig(BaseModel):
    """The whole configuration tree."""

    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("data")
    agents_dir: Path = Path("agents")
    db_filename: str = "alfred.db"
    timezone: str | None = None  # None = system local
    llm: ModelConfig = Field(default_factory=ModelConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_mcp_server_names(self) -> AlfredConfig:
        names = [server.name for server in self.mcp_servers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            # Two servers sharing a name would collapse into one tool
            # namespace, clobber each other's session, and route calls to the
            # wrong server under the wrong tier gate.
            raise ValueError(
                f"duplicate MCP server names: {', '.join(duplicates)}; each must be unique"
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename


def load_config(path: str | Path | None = None) -> AlfredConfig:
    """Load config from YAML, or defaults when no file exists yet."""
    candidates = (
        [Path(path)] if path else [Path("config/alfred.yaml"), Path("alfred.yaml")]
    )
    for candidate in candidates:
        if candidate.is_file():
            raw: Any = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ConfigError(f"Config root must be a mapping: {candidate}")
            try:
                return AlfredConfig.model_validate(raw)
            except ValueError as exc:
                raise ConfigError(f"Invalid config {candidate}: {exc}") from exc
    if path:
        raise ConfigError(f"Config file not found: {path}")
    return AlfredConfig()
