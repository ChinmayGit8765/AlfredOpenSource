"""Agent loader tests: forgiving discovery, strict materialisation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alfred.domain.schemas import (
    AgentBlueprint,
    AgentManifest,
    Lifecycle,
    Schedule,
    TargetShape,
    Triggers,
)
from alfred.errors import AlfredError
from alfred.runtime.agent_loader import load_agents, materialise_agent

REPO_AGENTS_DIR = Path(__file__).parent.parent / "agents"


def write_agent(
    agents_dir: Path,
    name: str,
    *,
    manifest: dict | None = None,
    prompt: str | None = "# Agent\nBe useful.",
) -> Path:
    folder = agents_dir / name
    folder.mkdir(parents=True)
    data = {"name": name, "description": f"{name} test agent"}
    if manifest:
        data.update(manifest)
    (folder / "manifest.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    if prompt is not None:
        (folder / "agent.md").write_text(prompt, encoding="utf-8")
    return folder


def test_valid_folder_loads(tmp_path: Path) -> None:
    write_agent(
        tmp_path,
        "training",
        manifest={
            "lifecycle": "forming",
            "triggers": {"keywords": ["train", "gym"]},
            "allowed_tools": ["current_time"],
        },
        prompt="# Training\nPlan the week.",
    )

    registry = load_agents(tmp_path)

    agent = registry.get("training")
    assert agent is not None
    assert agent.manifest.lifecycle is Lifecycle.FORMING
    assert agent.manifest.triggers.keywords == ["train", "gym"]
    assert agent.manifest.allowed_tools == ["current_time"]
    assert agent.prompt == "# Training\nPlan the week."
    assert agent.path == str(tmp_path / "training")


def test_extra_manifest_key_skips_folder_but_others_load(tmp_path: Path) -> None:
    write_agent(tmp_path, "good")
    write_agent(tmp_path, "broken", manifest={"surprise_field": True})

    registry = load_agents(tmp_path)

    assert registry.get("good") is not None
    assert registry.get("broken") is None
    assert len(registry.all()) == 1


def test_missing_agent_md_skips_folder(tmp_path: Path) -> None:
    write_agent(tmp_path, "promptless", prompt=None)
    write_agent(tmp_path, "fine")

    registry = load_agents(tmp_path)

    assert registry.get("promptless") is None
    assert registry.get("fine") is not None


def test_bad_yaml_skips_folder(tmp_path: Path) -> None:
    folder = tmp_path / "mangled"
    folder.mkdir()
    (folder / "manifest.yaml").write_text("name: [unclosed", encoding="utf-8")
    (folder / "agent.md").write_text("prompt", encoding="utf-8")

    registry = load_agents(tmp_path)

    assert registry.all() == []


def test_non_directory_entries_ignored(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("not an agent", encoding="utf-8")
    write_agent(tmp_path, "real")

    registry = load_agents(tmp_path)

    assert [a.manifest.name for a in registry.all()] == ["real"]


def test_missing_agents_dir_returns_empty_registry(tmp_path: Path) -> None:
    registry = load_agents(tmp_path / "nowhere")
    assert registry.all() == []


def test_materialise_round_trip_and_refuse_overwrite(tmp_path: Path) -> None:
    manifest = AgentManifest(
        name="reading",
        description="Nightly reading habit.",
        shape=TargetShape.HABIT,
        lifecycle=Lifecycle.FORMING,
        triggers=Triggers(keywords=["read"]),
        schedule=Schedule(kind="daily", time="21:00"),
        capacity_cost=1,
    )
    blueprint = AgentBlueprint(manifest=manifest, prompt_md="# Reading\nTen pages.")

    path = materialise_agent(tmp_path, blueprint)

    assert path == tmp_path / "reading"
    assert (path / "manifest.yaml").is_file()
    assert (path / "agent.md").read_text(encoding="utf-8") == "# Reading\nTen pages."
    assert (path / "state").is_dir()

    registry = load_agents(tmp_path)
    loaded = registry.get("reading")
    assert loaded is not None
    assert loaded.manifest == manifest
    assert loaded.prompt == blueprint.prompt_md

    with pytest.raises(AlfredError):
        materialise_agent(tmp_path, blueprint)


def test_real_repo_agents_load() -> None:
    registry = load_agents(REPO_AGENTS_DIR)

    names = {a.manifest.name for a in registry.all()}
    assert names == {"build", "study", "training"}
    for agent in registry.all():
        assert agent.prompt.strip()
        assert agent.manifest.allowed_tools
