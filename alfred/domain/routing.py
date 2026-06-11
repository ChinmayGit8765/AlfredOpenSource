"""Routing: which agents handle an inbound message.

Pure domain logic over the registry. Keyword triggers match
case-insensitively on word boundaries; always-on agents claim every
message. Lifecycle filtering rides on AgentRegistry.active(), so PAUSED
and RETIRED agents never route. An empty result is meaningful: no agent
claimed the message and the core falls back to general handling.
"""

from __future__ import annotations

import logging
import re

from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.schemas import InboundMessage

logger = logging.getLogger(__name__)


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    for keyword in keywords:
        keyword = keyword.strip()
        if not keyword:
            # An empty keyword would match everywhere; that is what
            # triggers.always is for, so skip it rather than over-route.
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE):
            return True
    return False


def route(message: InboundMessage, registry: AgentRegistry) -> list[LoadedAgent]:
    """Return the agents claiming this message, in deterministic order.

    Always-on agents come first, then keyword matches, each block sorted
    alphabetically by agent name, with no duplicates.
    """
    always: list[LoadedAgent] = []
    matched: list[LoadedAgent] = []
    # active() is already alphabetical and excludes PAUSED and RETIRED,
    # so appending in iteration order keeps each block sorted.
    for agent in registry.active():
        triggers = agent.manifest.triggers
        if triggers.always:
            always.append(agent)
        elif _matches_keywords(message.text, triggers.keywords):
            matched.append(agent)
    routed = always + matched
    if routed:
        logger.debug(
            "message %s routed to %s",
            message.id,
            ", ".join(agent.manifest.name for agent in routed),
        )
    return routed
