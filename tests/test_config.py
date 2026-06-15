"""Tests for config validation: typos fail loudly, MCP names stay routable."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alfred.config import AlfredConfig, McpServerConfig


def test_unknown_nested_key_is_rejected() -> None:
    # A typo in a safety knob must fail loudly, not be silently dropped while
    # the owner believes they changed the default.
    with pytest.raises(ValidationError):
        AlfredConfig.model_validate({"policy": {"auto_aprove_reversible": True}})


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AlfredConfig.model_validate({"notathing": 1})


def test_duplicate_mcp_server_names_rejected() -> None:
    # Duplicate names collapse into one tool namespace and misroute calls.
    with pytest.raises(ValidationError):
        AlfredConfig.model_validate(
            {
                "mcp_servers": [
                    {"name": "cal", "command": "a"},
                    {"name": "cal", "command": "b"},
                ]
            }
        )


def test_dotted_mcp_server_name_rejected() -> None:
    # The namespace splits on the first dot, so a dotted name is unroutable.
    with pytest.raises(ValidationError):
        McpServerConfig(name="my.server", command="x")


def test_valid_mcp_config_loads() -> None:
    cfg = AlfredConfig.model_validate(
        {"mcp_servers": [{"name": "calendar", "command": "mcp-cal"}]}
    )
    assert [s.name for s in cfg.mcp_servers] == ["calendar"]
