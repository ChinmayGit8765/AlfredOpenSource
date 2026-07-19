"""Integration tests for SqliteStoreAdapter against a real tmp_path database."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from alfred.adapters.sqlite_store import SqliteStoreAdapter
from alfred.domain.schemas import Collections


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStoreAdapter]:
    # Nested path exercises parent-directory creation.
    adapter = SqliteStoreAdapter(tmp_path / "data" / "alfred.db")
    yield adapter
    await adapter.close()


async def test_put_get_round_trip_includes_key(store: SqliteStoreAdapter) -> None:
    await store.put(Collections.PROFILE, "current", {"version": 1, "goals": ["ship"]})
    doc = await store.get(Collections.PROFILE, "current")
    assert doc == {"version": 1, "goals": ["ship"], "_key": "current"}


async def test_get_missing_returns_none(store: SqliteStoreAdapter) -> None:
    assert await store.get(Collections.PROFILE, "nope") is None


async def test_put_replaces_existing(store: SqliteStoreAdapter) -> None:
    await store.put(Collections.PROFILE, "current", {"version": 1})
    await store.put(Collections.PROFILE, "current", {"version": 2})
    doc = await store.get(Collections.PROFILE, "current")
    assert doc is not None
    assert doc["version"] == 2


async def test_delete_reports_existence(store: SqliteStoreAdapter) -> None:
    await store.put(Collections.PLANS, "p1", {"agent": "training"})
    assert await store.delete(Collections.PLANS, "p1") is True
    assert await store.get(Collections.PLANS, "p1") is None
    assert await store.delete(Collections.PLANS, "p1") is False


async def test_append_keys_strictly_increase(store: SqliteStoreAdapter) -> None:
    keys = [await store.append(Collections.AUDIT, {"event": f"e{i}"}) for i in range(5)]
    assert keys == sorted(keys)
    assert len(set(keys)) == 5


def _insert_raw(store: SqliteStoreAdapter, key: str, doc: str) -> None:
    store._conn.execute(
        "INSERT OR REPLACE INTO documents (collection, key, doc, created_at)"
        " VALUES (?, ?, ?, ?)",
        (Collections.PLANS, key, doc, "2026-01-01T00:00:00+00:00"),
    )
    store._conn.commit()


async def test_corrupt_or_non_object_rows_are_skipped_not_fatal(
    store: SqliteStoreAdapter,
) -> None:
    await store.put(Collections.PLANS, "good", {"agent": "training"})
    _insert_raw(store, "bad", "{not valid json")  # undecodable
    _insert_raw(store, "scalar", "42")  # valid JSON but not an object

    # A keyed get of a bad row degrades to None, never a raw JSONDecodeError
    # or TypeError.
    assert await store.get(Collections.PLANS, "bad") is None
    assert await store.get(Collections.PLANS, "scalar") is None

    # One bad row does not poison the whole collection: the good row survives.
    rows = await store.query(Collections.PLANS)
    assert [r["_key"] for r in rows] == ["good"]


async def test_query_orders_by_key_and_newest_first_reverses(
    store: SqliteStoreAdapter,
) -> None:
    for i in range(3):
        await store.append(Collections.OUTCOMES, {"n": i})
    oldest_first = await store.query(Collections.OUTCOMES)
    assert [d["n"] for d in oldest_first] == [0, 1, 2]
    newest_first = await store.query(Collections.OUTCOMES, newest_first=True)
    assert [d["n"] for d in newest_first] == [2, 1, 0]
    assert all("_key" in d for d in oldest_first + newest_first)


async def test_query_where_filters_top_level_fields(store: SqliteStoreAdapter) -> None:
    await store.append(Collections.OUTCOMES, {"agent": "training", "status": "done"})
    await store.append(Collections.OUTCOMES, {"agent": "study", "status": "done"})
    await store.append(Collections.OUTCOMES, {"agent": "training", "status": "missed"})

    training = await store.query(Collections.OUTCOMES, where={"agent": "training"})
    assert len(training) == 2
    assert all(d["agent"] == "training" for d in training)

    done_training = await store.query(
        Collections.OUTCOMES, where={"agent": "training", "status": "done"}
    )
    assert len(done_training) == 1

    none = await store.query(Collections.OUTCOMES, where={"agent": "ghost"})
    assert none == []


async def test_query_limit_applies_after_filtering(store: SqliteStoreAdapter) -> None:
    for i in range(6):
        await store.append(
            Collections.OBSERVATIONS, {"n": i, "kind": "event" if i % 2 else "insight"}
        )
    limited = await store.query(Collections.OBSERVATIONS, where={"kind": "event"}, limit=2)
    assert [d["n"] for d in limited] == [1, 3]


async def test_newest_first_with_limit_returns_most_recent(
    store: SqliteStoreAdapter,
) -> None:
    # Append keys are zero-padded time_ns, so key order is chronological and
    # the SQL LIMIT pushdown must yield exactly the N most recent docs.
    for i in range(5):
        await store.append(Collections.OUTCOMES, {"n": i})
    recent = await store.query(Collections.OUTCOMES, limit=2, newest_first=True)
    assert [d["n"] for d in recent] == [4, 3]


async def test_limit_pushdown_corrupt_row_shrinks_result_not_scan(
    store: SqliteStoreAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    # Documented edge of the where=None fast path: the SQL LIMIT fixes the
    # scan window, so a corrupt row inside it is skipped rather than scanned
    # past, and the call returns limit-1 docs instead of pulling row "04".
    for key in ("00", "01", "03", "04"):
        await store.put(Collections.PLANS, key, {"k": key})
    _insert_raw(store, "02", "{not valid json")

    with caplog.at_level(logging.WARNING, logger="alfred.adapters.sqlite_store"):
        rows = await store.query(Collections.PLANS, limit=4)

    assert [r["_key"] for r in rows] == ["00", "01", "03"]
    assert "skipping undecodable document" in caplog.text


async def test_concurrent_writers_and_readers_serialize(tmp_path: Path) -> None:
    # The single shared connection is guarded only by the asyncio.Lock in
    # _run; interleaved coroutines must serialize cleanly with no lost or
    # duplicated writes and no cross-talk between readers and writers.
    store = SqliteStoreAdapter(tmp_path / "concurrent.db")
    appended: list[str] = []

    async def append_loop(writer: int) -> None:
        for i in range(30):
            key = await store.append(Collections.AUDIT, {"writer": writer, "i": i})
            appended.append(key)

    async def put_loop(writer: int) -> None:
        for i in range(30):
            await store.put(Collections.PLANS, f"w{writer}-{i}", {"writer": writer, "i": i})

    async def query_loop() -> None:
        for _ in range(8):
            await store.query(Collections.AUDIT, newest_first=True, limit=10)

    try:
        await asyncio.gather(
            *(append_loop(w) for w in range(5)),
            *(put_loop(w) for w in range(2)),
            *(query_loop() for _ in range(3)),
        )

        assert len(appended) == 150
        assert len(set(appended)) == 150

        docs = await store.query(Collections.AUDIT)
        assert len(docs) == 150
        assert all("writer" in d and "i" in d for d in docs)

        doc = await store.get(Collections.PLANS, "w1-29")
        assert doc == {"writer": 1, "i": 29, "_key": "w1-29"}
    finally:
        await store.close()


async def test_unicode_and_nested_structures_survive(store: SqliteStoreAdapter) -> None:
    doc = {
        "title": "entrainement: cafe a 7h, puis 押忍",
        "emoji": "🏋️",
        "nested": {"levels": [{"deep": ["a", 1, None, True]}], "empty": {}},
    }
    await store.put(Collections.PLANS, "p-unicode", doc)
    loaded = await store.get(Collections.PLANS, "p-unicode")
    assert loaded == {**doc, "_key": "p-unicode"}


async def test_data_persists_across_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist" / "alfred.db"
    first = SqliteStoreAdapter(path)
    await first.put(Collections.PROFILE, "current", {"version": 3})
    key = await first.append(Collections.AUDIT, {"event": "boot"})
    await first.close()

    second = SqliteStoreAdapter(path)
    try:
        doc = await second.get(Collections.PROFILE, "current")
        assert doc == {"version": 3, "_key": "current"}
        audit = await second.query(Collections.AUDIT)
        assert [d["_key"] for d in audit] == [key]
    finally:
        await second.close()


async def test_query_unknown_collection_returns_empty(store: SqliteStoreAdapter) -> None:
    assert await store.query("never_written") == []
