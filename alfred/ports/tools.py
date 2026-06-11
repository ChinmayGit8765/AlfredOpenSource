"""ToolPort: the action surface.

Every capability ALFRED can exercise on the world (local tools today,
any MCP server tomorrow) is exposed through this one protocol. The
governance layer gates calls by CapabilityTier before they reach an
implementation, so the tier lives here, on the spec, not in the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class CapabilityTier(StrEnum):
    """How dangerous a tool action is. Confirmation requirements scale with it."""

    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    DESTRUCTIVE = "destructive"


class ToolSpec(BaseModel):
    """Description of one invokable tool."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    tier: CapabilityTier = CapabilityTier.DESTRUCTIVE
    source: str = "local"

    # tier defaults to DESTRUCTIVE deliberately: an unclassified tool gets
    # the strictest gate, never a free pass.


class ToolResult(BaseModel):
    """Outcome of a tool invocation."""

    ok: bool
    content: Any = None
    error: str | None = None


@runtime_checkable
class ToolPort(Protocol):
    """A source of invokable tools."""

    async def list_tools(self) -> list[ToolSpec]:
        """Return the specs of every tool this source provides."""
        ...

    async def invoke(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        """Invoke a tool by name. Raises ToolNotFoundError for unknown names."""
        ...
