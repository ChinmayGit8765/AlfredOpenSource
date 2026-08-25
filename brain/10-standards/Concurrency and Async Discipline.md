---
tags: [standard, architecture]
status: partial
applies-to: [alfred/runtime, alfred/adapters/sqlite_store.py]
---

# Concurrency and Async Discipline

## What it is

Rules for a single-process asyncio application: what may block, what must
be serialized, and how shared mutable state is protected when several
tasks can touch it.

## Why it matters here

ALFRED serves one owner, which sounds like it removes the problem. It does
not. Several independent tasks run at once on one event loop: a Discord
gateway, a Telegram poller, an HTTP transport, and a heartbeat that fires
on a schedule. They all reach the same core, the same store, and the same
pending-action list.

The concrete hazard is not throughput, it is **double execution of a gated
action**. `confirm <id>` arriving on two channels, or arriving while the
heartbeat is mid-run, is a read-modify-write race on the pending-actions
collection. Losing that race means a destructive tool runs twice from one
confirmation, which is exactly the outcome the whole governance model
exists to prevent.

Second hazard: **a blocking call on the event loop**. sqlite3 is
synchronous. A long query on the loop thread stalls the Discord heartbeat,
which makes the gateway drop the connection, which looks like a network
fault and is not.

## What good looks like

- One coarse lock around the whole inbound and scheduled handler, not fine
  grained locks around each mutation. Throughput is a non-goal for a
  single-owner system; coherence is the point, and one lock is provably
  correct where five interacting ones are not.
- Blocking I/O runs in a thread (`asyncio.to_thread`), with a lock if the
  resource is not thread safe.
- Waiting is event-driven, not polled. A `while not flag: await sleep(1)`
  is a busy wait that also adds up to a second of latency to whatever it
  guards.
- Every long-lived task is **named**, so a failure message can say which
  one died.
- Cancellation is handled: `asyncio.CancelledError` is not an error to
  report, it is a shutdown to complete.

## What bad looks like

- `time.sleep` anywhere in an async path.
- A shared connection touched from `to_thread` workers with no lock. It
  works until two writes overlap and sqlite raises from a thread nobody is
  watching.
- Fire-and-forget `create_task` with no reference kept: the task can be
  garbage collected mid-flight, and its exception is swallowed.
- Double-checked locking written without the second check, or with the
  second check deleted by someone who read it as redundant.

## How ALFRED does it

`AlfredCore` holds one `asyncio.Lock` that serializes every inbound message
and every scheduled trigger, with a comment stating why. The sqlite adapter
runs all statements through `asyncio.to_thread` behind an `asyncio.Lock`,
so the single shared connection is never concurrent. `_ensure_session` in
the MCP adapter is a correct double-checked lock, annotated because a type
checker reads the second check as dead code.

Shutdown is an `asyncio.Event`: `alfred stop` sets it, the supervisor
awaits it. This replaced a one-second poll loop.

## Verification

Partial, and honestly so. The lock's existence is asserted by
`tests/test_core.py`'s concurrent-confirm tests. The absence of blocking
calls in async paths is covered by ruff's `ASYNC` rules, which catch the
common shapes (`ASYNC110` busy waits, `ASYNC240` blocking path calls) but
not an arbitrary blocking library call.

Open gap: no test proves the store lock is actually held under concurrent
writes. See [[Gap Register]].

## Sources

- The `asyncio` developer documentation on task references and cancellation.
- Trio's "structured concurrency" essays, for why unnamed background tasks
  are a design smell even outside Trio.
