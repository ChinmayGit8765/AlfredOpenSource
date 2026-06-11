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
from pydantic import BaseModel, ConfigDict, Field

from alfred.errors import ConfigError


class ModelConfig(BaseModel):
    """Local model backend settings."""

    host: str = "http://127.0.0.1:11434"  # localhost by default; sovereignty
    name: str = "qwen3:8b"
    fallbacks: list[str] = Field(default_factory=lambda: ["qwen2.5:7b", "llama3.1:8b"])
    temperature: float = 0.4


class DiscordConfig(BaseModel):
    """Discord transport settings. The token comes from the environment."""

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


class HeartbeatConfig(BaseModel):
    """Proactive scheduler settings."""

    enabled: bool = True
    tick_seconds: int = Field(default=60, ge=5)
    quiet_hours: str = "22:30-07:30"  # "HH:MM-HH:MM" local; no proactive pings
    reflection_days: int = 7  # cadence of periodic Conductor reflection


class PolicyConfig(BaseModel):
    """Governance knobs. Defaults are the safe direction."""

    auto_approve_reversible: bool = True  # reversible writes run, but are audited
    dry_run_cross_system: bool = True  # multi-system workflows preview first
    pending_action_ttl_hours: int = 24


class McpServerConfig(BaseModel):
    """One MCP server ALFRED may connect to (phase 6, the action layer)."""

    name: str
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
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)

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
