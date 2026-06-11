"""StorePort: persistence as a small document store.

Collections hold JSON documents. Two access patterns cover everything
ALFRED needs: keyed documents (profile, manifests, sessions) and
append-only logs (outcomes, observations, audit). Keeping the port
generic means the domain never learns SQL and the backend stays swappable.

Conventions:
- Documents are JSON-serialisable mappings.
- Returned documents include their key under "_key".
- append() generates a time-ordered key so newest_first works naturally.
- query(where=...) matches top-level fields by equality only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StorePort(Protocol):
    """A document store with keyed and append-only collections."""

    async def put(self, collection: str, key: str, doc: Mapping[str, Any]) -> None:
        """Insert or replace the document at key."""
        ...

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        """Return the document at key (with "_key" included), or None."""
        ...

    async def delete(self, collection: str, key: str) -> bool:
        """Delete the document at key. Returns True if it existed."""
        ...

    async def append(self, collection: str, doc: Mapping[str, Any]) -> str:
        """Add a document under a generated time-ordered key; return the key."""
        ...

    async def query(
        self,
        collection: str,
        *,
        where: Mapping[str, Any] | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Return documents (each with "_key") filtered by top-level equality."""
        ...
