"""Ports: the interfaces the domain depends on.

The domain layer imports only from this package (and stdlib/pydantic).
Adapters implement these protocols; the composition root wires them in.
Nothing in this package may import from alfred.domain, alfred.adapters,
or alfred.runtime.
"""

from alfred.ports.clock import ClockPort
from alfred.ports.model import ModelMessage, ModelOptions, ModelPort
from alfred.ports.store import StorePort
from alfred.ports.tools import CapabilityTier, ToolPort, ToolResult, ToolSpec
from alfred.ports.transport import OutboundMessage, TransportPort

__all__ = [
    "CapabilityTier",
    "ClockPort",
    "ModelMessage",
    "ModelOptions",
    "ModelPort",
    "OutboundMessage",
    "StorePort",
    "ToolPort",
    "ToolResult",
    "ToolSpec",
    "TransportPort",
]
