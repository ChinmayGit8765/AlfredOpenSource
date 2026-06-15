"""Tests for the alfred CLI argument parser.

The subcommand wiring (flags, required command) is pure and worth pinning;
the async command bodies need a real model/TTY and are exercised by the
composition smoke test and manual runs instead.
"""

from __future__ import annotations

import pytest

from alfred.runtime.cli import _build_parser


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
