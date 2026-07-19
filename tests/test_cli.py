"""Tests for the alfred CLI: the argument parser and doctor's pure helpers.

The subcommand wiring (flags, required command) is pure and worth pinning;
the async command bodies need a real model/TTY and are exercised by the
composition smoke test and manual runs instead.
"""

from __future__ import annotations

import pytest

from alfred.adapters.mcp_tools import McpServerStatus
from alfred.ports.tools import CapabilityTier, ToolSpec
from alfred.runtime.cli import _build_parser, _mcp_check_lines


def test_chat_accepts_fake_flag() -> None:
    args = _build_parser().parse_args(["chat", "--fake"])
    assert args.command == "chat"
    assert args.fake is True


def test_chat_defaults_to_real_mode() -> None:
    args = _build_parser().parse_args(["chat"])
    assert args.fake is False


def test_doctor_and_run_parse() -> None:
    assert _build_parser().parse_args(["doctor"]).command == "doctor"
    assert _build_parser().parse_args(["run"]).command == "run"


def test_agents_list_is_a_nested_subcommand() -> None:
    args = _build_parser().parse_args(["agents", "list"])
    assert args.command == "agents"
    assert args.agents_command == "list"


def test_demo_roundtrip_accepts_fake() -> None:
    args = _build_parser().parse_args(["demo-roundtrip", "--fake"])
    assert args.command == "demo-roundtrip" and args.fake is True


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args([])


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["frobnicate"])


# --- doctor's MCP report ----------------------------------------------------


def _spec(name: str, tier: CapabilityTier) -> ToolSpec:
    return ToolSpec(name=name, description="", tier=tier, source="mcp:calendar")


def test_mcp_lines_ok_when_connected_and_fully_classified() -> None:
    status = McpServerStatus(
        name="calendar",
        connected=True,
        specs=(
            _spec("calendar.list-events", CapabilityTier.READ_ONLY),
            _spec("calendar.create-event", CapabilityTier.REVERSIBLE_WRITE),
            _spec("calendar.delete-event", CapabilityTier.DESTRUCTIVE),
        ),
        unclassified=(),
    )

    [(level, text)] = _mcp_check_lines([status])

    assert level == "ok"
    assert "'calendar' up: 3 tool(s)" in text
    assert "1 read_only, 1 reversible_write, 1 destructive" in text


def test_mcp_lines_warn_and_name_the_unclassified() -> None:
    status = McpServerStatus(
        name="calendar",
        connected=True,
        specs=(_spec("calendar.manage-accounts", CapabilityTier.DESTRUCTIVE),),
        unclassified=("manage-accounts",),
    )

    [(level, text)] = _mcp_check_lines([status])

    assert level == "warn"
    assert "manage-accounts" in text
    assert "asks first" in text


def test_mcp_lines_warn_when_server_did_not_connect() -> None:
    status = McpServerStatus(
        name="calendar", connected=False, specs=(), unclassified=()
    )

    [(level, text)] = _mcp_check_lines([status])

    assert level == "warn"
    assert "did not connect" in text


def test_mcp_lines_one_per_server() -> None:
    up = McpServerStatus(name="cal", connected=True, specs=(), unclassified=())
    down = McpServerStatus(name="files", connected=False, specs=(), unclassified=())

    lines = _mcp_check_lines([up, down])

    assert [level for level, _ in lines] == ["ok", "warn"]
