"""Tests for alfred.domain.registry: in-memory agent registry and manifests."""

from __future__ import annotations

import pytest

from alfred.domain.registry import AgentRegistry, LoadedAgent, parse_manifest
from alfred.domain.schemas import AgentManifest, Lifecycle, Triggers
from alfred.errors import ManifestError


def make_agent(
    name: str,
    *,
    lifecycle: Lifecycle = Lifecycle.ESTABLISHED,
    keywords: list[str] | None = None,
    always: bool = False,
) -> LoadedAgent:
    manifest = AgentManifest(
        name=name,
        description=f"{name} test agent",
        lifecycle=lifecycle,
        triggers=Triggers(keywords=keywords or [], always=always),
    )
    return LoadedAgent(manifest=manifest, prompt=f"You are {name}.")


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


def test_add_then_get_returns_agent():
    registry = AgentRegistry()
    agent = make_agent("training")

    registry.add(agent)

    assert registry.get("training") is agent


def test_get_unknown_name_returns_none():
    registry = AgentRegistry()
    assert registry.get("ghost") is None


def test_add_same_name_replaces_existing():
    registry = AgentRegistry()
    registry.add(make_agent("study", lifecycle=Lifecycle.FORMING))
    replacement = make_agent("study", lifecycle=Lifecycle.ESTABLISHED)

    registry.add(replacement)

    assert registry.get("study") is replacement
    assert len(registry.all()) == 1


def test_constructor_seeds_initial_agents():
    agents = [make_agent("beta"), make_agent("alpha")]
    registry = AgentRegistry(agents)

    assert [a.manifest.name for a in registry.all()] == ["alpha", "beta"]


def test_all_returns_agents_sorted_by_name():
    registry = AgentRegistry()
    for name in ["zeta", "alpha", "mike"]:
        registry.add(make_agent(name))

    assert [a.manifest.name for a in registry.all()] == ["alpha", "mike", "zeta"]


def test_remove_existing_returns_true_and_deletes():
    registry = AgentRegistry([make_agent("training")])

    assert registry.remove("training") is True
    assert registry.get("training") is None
    assert registry.all() == []


def test_remove_missing_returns_false():
    registry = AgentRegistry()
    assert registry.remove("ghost") is False


def test_active_excludes_paused_and_retired_includes_lapsing_and_forming():
    registry = AgentRegistry(
        [
            make_agent("paused-one", lifecycle=Lifecycle.PAUSED),
            make_agent("retired-one", lifecycle=Lifecycle.RETIRED),
            make_agent("lapsing-one", lifecycle=Lifecycle.LAPSING),
            make_agent("forming-one", lifecycle=Lifecycle.FORMING),
            make_agent("established-one", lifecycle=Lifecycle.ESTABLISHED),
        ]
    )

    active_names = [a.manifest.name for a in registry.active()]

    assert "paused-one" not in active_names
    assert "retired-one" not in active_names
    assert "lapsing-one" in active_names
    assert "forming-one" in active_names
    assert "established-one" in active_names


# ---------------------------------------------------------------------------
# parse_manifest
# ---------------------------------------------------------------------------


def _valid_manifest_dict() -> dict:
    return {
        "name": "training",
        "description": "weekly training plans",
        "lifecycle": "forming",
        "triggers": {"keywords": ["gym", "workout"], "always": False},
        "allowed_tools": ["current_time"],
        "capacity_cost": 4,
    }


def test_parse_manifest_round_trips_valid_dict():
    manifest = parse_manifest(_valid_manifest_dict())

    assert isinstance(manifest, AgentManifest)
    assert manifest.name == "training"
    assert manifest.lifecycle is Lifecycle.FORMING
    assert manifest.triggers.keywords == ["gym", "workout"]

    # Dumped form must validate back to an equal manifest.
    assert parse_manifest(manifest.model_dump(mode="json")) == manifest


def test_parse_manifest_rejects_unknown_extra_key():
    raw = _valid_manifest_dict()
    raw["allowed_toolz"] = ["everything"]  # typo'd key must fail loudly

    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(raw)

    message = str(excinfo.value)
    assert "invalid agent manifest" in message
    assert "allowed_toolz" in message


def test_parse_manifest_rejects_bad_name_pattern():
    raw = _valid_manifest_dict()
    raw["name"] = "Bad Name!"

    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(raw)

    message = str(excinfo.value)
    assert "invalid agent manifest" in message
    assert "name" in message
