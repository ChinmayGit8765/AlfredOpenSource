"""TransportPort: outbound messaging.

Inbound flow is inverted: the transport adapter receives platform events
and calls into the core's handler. This port only covers what the domain
needs to initiate, sending a message to the owner (replies, proactive
check-ins, confirmation requests).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class OutboundMessage(BaseModel):
    """A message for the owner. channel is transport-specific routing."""

    channel: str
    text: str


@runtime_checkable
class TransportPort(Protocol):
    """Delivers messages to the owner."""

    async def send(self, message: OutboundMessage) -> None:
        """Deliver the message. Raises TransportError on failure."""
        ...
