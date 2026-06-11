"""Filesystem to registry: discovering and materialising agent folders.

The registry is pure domain; this module is the runtime edge that turns
agents/<name>/ folders into LoadedAgent objects and writes approved
blueprints back to disk. Discovery is forgiving (a broken folder is
logged and skipped, never fatal) while materialisation is strict (it
refuses to overwrite an existing agent).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from alfred.domain.registry import AgentRegistry, LoadedAgent, parse_manifest
from alfred.domain.schemas import AgentBlueprint, AgentManifest
from alfred.errors import AlfredError, ManifestError

logger = logging.getLogger(__name__)

_MANIFEST_FILE = "manifest.yaml"
_PROMPT_FILE = "agent.md"
_STATE_DIR = "state"


def render_manifest_yaml(manifest: AgentManifest) -> str:
    """Canonical on-disk YAML for a manifest, shared by every writer."""
    return yaml.safe_dump(
        manifest.model_dump(mode="json", exclude_none=True), sort_keys=False
    )


def _load_one(folder: Path) -> LoadedAgent:
    raw = yaml.safe_load((folder / _MANIFEST_FILE).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest root must be a mapping: {folder / _MANIFEST_FILE}")
    manifest = parse_manifest(raw)
    prompt = (folder / _PROMPT_FILE).read_text(encoding="utf-8")
    return LoadedAgent(manifest=manifest, prompt=prompt, path=str(folder))


def load_agents(agents_dir: str | Path) -> AgentRegistry:
    """Scan agents_dir for agent folders and return the populated registry.

    Only immediate subdirectories containing manifest.yaml are considered.
    A folder that fails to load for any reason (bad YAML, invalid manifest,
    missing agent.md) is logged at warning and skipped, never fatal.
    """
    registry = AgentRegistry()
    root = Path(agents_dir)
    if not root.is_dir():
        logger.warning("agents directory does not exist: %s", root)
        return registry

    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not (folder / _MANIFEST_FILE).is_file():
            continue
        try:
            agent = _load_one(folder)
        except Exception as exc:
            logger.warning("skipping agent folder %s: %s", folder, exc)
            continue
        registry.add(agent)
        logger.info(
            "loaded agent %s (lifecycle=%s)",
            agent.manifest.name,
            agent.manifest.lifecycle.value,
        )
    return registry


def materialise_agent(agents_dir: str | Path, blueprint: AgentBlueprint) -> Path:
    """Write an approved blueprint to disk as a loadable agent folder.

    Refuses to overwrite: an existing folder means an existing agent, and
    silently clobbering its prompt or manifest would be destructive.
    """
    folder = Path(agents_dir) / blueprint.manifest.name
    if folder.exists():
        raise AlfredError(f"agent folder already exists, refusing to overwrite: {folder}")
    folder.mkdir(parents=True)
    (folder / _MANIFEST_FILE).write_text(
        render_manifest_yaml(blueprint.manifest), encoding="utf-8"
    )
    (folder / _PROMPT_FILE).write_text(blueprint.prompt_md, encoding="utf-8")
    (folder / _STATE_DIR).mkdir()
    logger.info("materialised agent %s at %s", blueprint.manifest.name, folder)
    return folder
