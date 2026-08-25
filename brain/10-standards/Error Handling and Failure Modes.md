---
tags: [standard, architecture, operations]
status: enforced
applies-to: [alfred/errors.py, alfred/runtime/core.py, alfred/runtime/cli.py]
---

# Error Handling and Failure Modes

## What it is

A policy for what happens when something goes wrong: which errors are
caught, where they are translated, what the owner is told, and what the
system does next.

## Why it matters here

There is no operator. Nobody reads a dashboard, nobody gets paged, and the
owner is not going to open a stack trace. A personal system that fails
badly is not debugged, it is uninstalled.

Two failure classes matter and they need opposite treatment.

**Environmental failures** are normal: Ollama is not running, the Discord
token expired, the disk is full, an MCP server died. These must produce one
short honest sentence naming the thing and the fix, and the system should
keep doing whatever else still works.

**Invariant failures** are bugs: a plan that validates against no schema, a
tool that reached the port without a dispatcher, a corrupt row. These must
be loud, must not be papered over, and must never silently degrade the
guarantees.

The mistake that ruins a system like this is treating the second class like
the first: swallowing an exception, logging at debug, and continuing with a
half-built state.

## What good looks like

- One error hierarchy rooted at a project exception, with meaningful
  subclasses (`ConfigError`, `StoreError`, `ToolNotAllowedError`,
  `StructuredCallError`).
- Adapters translate foreign exceptions at the boundary. A `sqlite3.Error`
  becomes a `StoreError`; the domain never catches a driver exception.
- **Tolerant reads, strict writes.** A single unparseable stored row is
  logged and skipped, because one bad document must not take down every
  read of a collection and cascade into prompt assembly. A bad write is
  refused.
- The owner-facing message names the thing and the fix: "Discord rejected
  the bot token. Check the `ALFRED_DISCORD_TOKEN` environment variable."
- Log records never contain owner data. The row that failed is identified
  by key, not by content.
- A `doctor` command that checks readiness before anything runs, so the
  first failure is a diagnosis rather than a traceback.

## What bad looks like

- `except Exception: pass`.
- A retry loop around something that will never succeed.
- Errors that surface as a raw traceback in a chat message.
- A message that says "an error occurred" and nothing more.
- Logging the document that failed to parse, which is how memories end up
  in a log file the owner then pastes into a bug report.

## How ALFRED does it

`alfred/errors.py` defines the hierarchy. `SqliteStoreAdapter` wraps every
`sqlite3.Error` in `StoreError`. `_decode_doc` logs and skips an
undecodable row by key with the content deliberately omitted, with a
comment saying why. `load_or_none` does the same for schema drift: a row
written by an older release degrades to a logged skip rather than crashing
every read of its collection.

`structured_call` retries a schema-invalid model reply with a repair prompt
a bounded number of times, then raises `StructuredCallError` rather than
returning something half-parsed.

## Verification

- `tests/test_sqlite_store.py` covers corrupt-row tolerance.
- `tests/test_schemas.py` covers `load_or_none` against drifted documents.
- `tests/test_structured.py` covers the retry and the eventual raise.
- ruff's `BLE` (blind except) rules are not currently enabled. See
  [[Gap Register]].

## Sources

- Google SRE Workbook, on graceful degradation, adapted for a system with
  no operator.
- Postel's law applied narrowly: tolerant in what you read from your own
  old writes, strict in what you accept from a model or a user.
