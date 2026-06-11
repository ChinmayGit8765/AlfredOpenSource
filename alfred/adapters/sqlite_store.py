"""SqliteStoreAdapter: StorePort over stdlib sqlite3.

One table of JSON documents keyed by (collection, key). The sync sqlite3
driver runs inside asyncio.to_thread, serialized by an asyncio.Lock so the
single shared connection is never touched concurrently. Where-filtering
happens in Python on the loaded documents: collections are small and
simple-and-correct beats clever SQL json_extract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from alfred.errors import StoreError

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    collection TEXT NOT NULL,
    key        TEXT NOT NULL,
    doc        TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (collection, key)
)
"""


class SqliteStoreAdapter:
    """StorePort backed by a single sqlite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # One connection shared across to_thread workers; the lock in
            # _run guarantees it is only ever used from one thread at a time.
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"failed to open sqlite store at {self._path}: {exc}") from exc
        self._lock = asyncio.Lock()

    async def _run(self, fn: Callable[[], Any]) -> Any:
        async with self._lock:
            try:
                return await asyncio.to_thread(fn)
            except sqlite3.Error as exc:
                raise StoreError(f"sqlite operation failed: {exc}") from exc

    @staticmethod
    def _now_iso() -> str:
        # Adapter layer is the I/O boundary; wall clock is fine here.
        return datetime.now(timezone.utc).isoformat()

    async def put(self, collection: str, key: str, doc: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(doc), ensure_ascii=False)
        created_at = self._now_iso()

        def work() -> None:
            self._conn.execute(
                "INSERT OR REPLACE INTO documents (collection, key, doc, created_at)"
                " VALUES (?, ?, ?, ?)",
                (collection, key, payload, created_at),
            )
            self._conn.commit()

        await self._run(work)

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        def work() -> str | None:
            row = self._conn.execute(
                "SELECT doc FROM documents WHERE collection = ? AND key = ?",
                (collection, key),
            ).fetchone()
            return row[0] if row else None

        raw = await self._run(work)
        if raw is None:
            return None
        doc: dict[str, Any] = json.loads(raw)
        doc["_key"] = key
        return doc

    async def delete(self, collection: str, key: str) -> bool:
        def work() -> int:
            cursor = self._conn.execute(
                "DELETE FROM documents WHERE collection = ? AND key = ?",
                (collection, key),
            )
            self._conn.commit()
            return cursor.rowcount

        return bool(await self._run(work))

    async def append(self, collection: str, doc: Mapping[str, Any]) -> str:
        # Nanosecond wall clock keys sort chronologically as strings; the
        # uuid suffix guards against same-instant collisions.
        key = f"{time.time_ns():020d}-{uuid4().hex[:6]}"
        await self.put(collection, key, doc)
        return key

    async def query(
        self,
        collection: str,
        *,
        where: Mapping[str, Any] | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        order = "DESC" if newest_first else "ASC"

        def work() -> list[tuple[str, str]]:
            return self._conn.execute(
                f"SELECT key, doc FROM documents WHERE collection = ? ORDER BY key {order}",
                (collection,),
            ).fetchall()

        rows = await self._run(work)
        results: list[dict[str, Any]] = []
        for key, raw in rows:
            doc: dict[str, Any] = json.loads(raw)
            if where and any(doc.get(k) != v for k, v in where.items()):
                continue
            doc["_key"] = key
            results.append(doc)
            if limit is not None and len(results) >= limit:
                break
        return results

    async def close(self) -> None:
        await self._run(self._conn.close)
