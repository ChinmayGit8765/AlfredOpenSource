"""The agent registry: agents in memory.

Pure domain: the registry knows nothing about folders or YAML. Scanning
the agents/ directory and turning files into LoadedAgent objects is the
runtime's job (runtime/agent_loader.py); the registry just holds and
filters what it is given.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from alfred.domain.schemas import AgentManifest, Lifecycle
from alfred.errors import ManifestError

_INACTIVE = frozenset({Lifecycle.PAUSED, Lifecycle.RETIRED})


class LoadedAgent(BaseModel):
    """An agent ready to run: validated manifest plus its behaviour prompt."""

    manifest: AgentManifest
    prompt: str
    path: str = ""


class AgentRegistry:
    """All known agents, keyed by manifest name."""

    def __init__(self, agents: list[LoadedAgent] | None = None) -> None:
        self._agents: dict[str, LoadedAgent] = {}
        for agent in agents or []:
            self.add(agent)

    def add(self, agent: LoadedAgent) -> None:
        self._agents[agent.manifest.name] = agent

    def get(self, name: str) -> LoadedAgent | None:
        return self._agents.get(name)

    def all(self) -> list[LoadedAgent]:
        return sorted(self._agents.values(), key=lambda a: a.manifest.name)

    def active(self) -> list[LoadedAgent]:
        """Agents that may route, plan, and be scheduled."""
        return [a for a in self.all() if a.manifest.lifecycle not in _INACTIVE]

    def remove(self, name: str) -> bool:
        return self._agents.pop(name, None) is not None


def parse_manifest(raw: dict) -> AgentManifest:
    """Validate raw manifest data, converting pydantic noise to a clear error."""
    try:
        return AgentManifest.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise ManifestError(f"invalid agent manifest: {problems}") from exc
