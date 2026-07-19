"""The memory layer: explicit, recallable facts about the owner's life.

This is what lets ALFRED refer to things. The owner (or an agent, through
a gated tool) files a fact once; every later conversation that touches the
topic gets it back, and every agent run is briefed with the memories
relevant to the message at hand, so the whole system stays coherent with
one person's actual life.

Recall is deterministic keyword scoring, not embeddings: at personal scale
(thousands of memories, not millions) token overlap with a recency bonus
finds the right facts, runs offline in microseconds, needs no model, and
its failures are explainable. A vector index can slot in behind the same
service later without touching any caller.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from alfred.domain.schemas import Collections, Memory, load_or_none
from alfred.ports.clock import ClockPort
from alfred.ports.store import StorePort

_TOKEN = re.compile(r"[a-z0-9]{2,}")

# Words that carry no recall signal; kept deliberately small because at
# personal scale false positives are cheaper than missed memories.
_STOPWORDS = frozenset(
    """
    the and for with that this what when where who how did does you your
    about have has was were are is its can could should would tell know
    me my our out not from will them they then than
    """.split()
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def tokenize(text: str) -> set[str]:
    """Lowercase content tokens; the unit recall scoring works in."""
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


class MemoryService:
    """Files, scores, and renders memories. Append-only plus explicit forget."""

    def __init__(self, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    async def remember(
        self,
        text: str,
        *,
        source: str = "owner",
        kind: str = "fact",
        tags: list[str] | None = None,
    ) -> Memory:
        memory = Memory.model_validate(
            {
                "text": text.strip(),
                "source": source,
                "kind": kind,
                "tags": tags or [],
                "at": self._clock.now(),
            }
        )
        await self._store.append(Collections.MEMORIES, memory.model_dump(mode="json"))
        return memory

    async def recall(self, query: str, *, limit: int = 5) -> list[Memory]:
        """Memories relevant to the query, best first.

        Score is token overlap between query and memory text plus tags;
        ties break toward the newer memory. Zero overlap never surfaces.
        """
        wanted = tokenize(query)
        if not wanted:
            return []
        scored: list[tuple[float, datetime, Memory]] = []
        for memory in await self._all():
            have = tokenize(memory.text) | {t.lower() for t in memory.tags}
            overlap = len(wanted & have)
            if overlap == 0:
                continue
            score = overlap / len(wanted)
            scored.append((score, memory.at or _EPOCH, memory))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [memory for _, _, memory in scored[:limit]]

    async def recent(self, limit: int = 10) -> list[Memory]:
        docs = await self._store.query(
            Collections.MEMORIES, limit=limit, newest_first=True
        )
        loaded = [load_or_none(Memory, doc, source=Collections.MEMORIES) for doc in docs]
        return [memory for memory in loaded if memory is not None]

    async def forget(self, memory_id: str) -> bool:
        """Delete one memory by its id field. The owner's data, the owner's call."""
        docs = await self._store.query(Collections.MEMORIES)
        for doc in docs:
            if doc.get("id") == memory_id:
                return await self._store.delete(Collections.MEMORIES, doc["_key"])
        return False

    async def context_for(self, text: str, *, limit: int = 4) -> str:
        """A compact prompt block of memories relevant to this message.

        Empty string when nothing is relevant, so callers can skip the
        section entirely instead of prompting with noise.
        """
        memories = await self.recall(text, limit=limit)
        if not memories:
            return ""
        lines = ["Relevant things the owner has told you before:"]
        for memory in memories:
            when = f" ({memory.at.date().isoformat()})" if memory.at else ""
            lines.append(f"- [{memory.kind}] {memory.text}{when}")
        return "\n".join(lines)

    async def _all(self) -> list[Memory]:
        docs = await self._store.query(Collections.MEMORIES)
        loaded = [load_or_none(Memory, doc, source=Collections.MEMORIES) for doc in docs]
        return [memory for memory in loaded if memory is not None]


def _without_key(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != "_key"}
