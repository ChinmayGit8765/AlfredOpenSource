---
tags: [standard, data]
status: partial
applies-to: [alfred/adapters/sqlite_store.py, alfred/domain/schemas.py]
---

# Data Durability and Migration

## What it is

Keeping the owner's data intact across crashes, power loss, and the
project's own schema changes.

## Why it matters here

There is one copy. No replica, no managed backup, no support team with a
snapshot. The sqlite file on the owner's disk is the entire system of
record for everything they have told ALFRED, and if it is corrupted or
silently unreadable, that history is gone.

The subtler risk is **schema drift over releases**. Documents written by
version 0.1 are read by version 0.4. If a field became required, or an enum
gained a value the old code cannot parse, every read of that collection
raises. In this architecture a raised read cascades: prompt assembly pulls
recent outcomes, outcomes fail to load, the agent run dies, and the owner
sees a broken system with no idea that one legacy row is the cause.

## What good looks like

- WAL journal mode. Better crash behaviour and readers that do not block a
  writer.
- **Tolerant reads.** A document that no longer validates is logged and
  skipped, never fatal. One bad row must not poison a collection.
- Additive schema evolution: new fields optional with defaults, enum values
  added rather than renamed, nothing removed without a migration.
- Keys that sort chronologically, so "recent" is a range scan rather than a
  full read and a sort.
- Bounded reads. `fetchmany` batches rather than `fetchall` on a collection
  that grows for years.
- A documented backup story. "Copy this file" counts, if it is written
  down, and if it mentions the `-wal` and `-shm` companions.
- A restore that someone has actually tried.

## What bad looks like

- `fetchall()` on the messages collection.
- A required field added to a stored model with no default, which breaks
  every historical row at once and passes every test, because the tests
  write current-shape documents.
- Silent data loss dressed as tolerance: skipping a bad row is right,
  skipping it without a log line is not.
- A `LIMIT` pushed into SQL underneath a Python-side filter, so the filter
  starves for candidates and returns fewer rows than asked for, silently.

## How ALFRED does it

`SqliteStoreAdapter` sets `PRAGMA journal_mode=WAL`, runs every statement
in a worker thread behind a lock, streams reads in `fetchmany(200)`
batches, and decodes in the worker so json parsing stays off the event
loop. Append keys are `f"{time.time_ns():020d}-{uuid4().hex[:6]}"`, so they
sort chronologically as strings with a collision guard.

`load_or_none` is the tolerant-read primitive: it validates one stored
document, logs and returns `None` on failure with the contents kept out of
the log, and callers filter. Its docstring states the cascade it exists to
prevent.

The `LIMIT` pushdown is applied only when there is no where-filter, with a
comment documenting the edge case it creates: a corrupt row inside the
limit window means the fast path can return fewer than `limit` documents.

## Verification

`tests/test_sqlite_store.py` covers corrupt rows, the limit pushdown edge
case, and ordering. `tests/test_schemas.py` covers `load_or_none` against
drifted shapes.

Open gaps, both real:

- **No migration mechanism and no schema version stamp on documents.**
  Tolerant reads handle drift by discarding, which is right for a
  malformed row and wrong for a field rename: the data is readable, just
  differently shaped, and it gets dropped.
- **No backup guidance in the docs.** `SECURITY.md` says to back up `data/`
  like a password manager; nothing says how, or mentions `-wal`.

See [[Gap Register]].

## Sources

- SQLite documentation on WAL mode and on file-format durability.
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 4, on
  backward and forward compatibility as separate properties.
