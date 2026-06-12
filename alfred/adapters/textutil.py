"""Shared text helpers for transport adapters.

Not an adapter: adapters never import each other, but pure text plumbing
they all need lives here once.
"""

from __future__ import annotations


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    """Split text into pieces of at most limit chars, preferring newlines.

    A cut at a newline drops that newline (it becomes the message
    boundary); a single overlong line is hard-split at the limit. Empty
    text yields no chunks because messaging platforms reject empty sends.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        else:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1 :]
    if remaining:
        chunks.append(remaining)
    return chunks
