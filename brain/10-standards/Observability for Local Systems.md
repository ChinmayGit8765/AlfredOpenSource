---
tags: [standard, operations]
status: partial
applies-to: [alfred/logging_setup.py, alfred/domain/governance.py]
---

# Observability for Local Systems

## What it is

Knowing what the system did and why, on a machine with no metrics backend,
no tracing collector, no dashboard, and an operator who is also the user.

## Why it matters here

Standard observability assumes a place to send data. There isn't one, and
building one would violate [[ADR-0006 No telemetry, ever]]. So the
requirements invert: everything stays local, and the audience for it is a
person, not a query engine.

Two distinct needs, often confused:

**Debugging.** Something broke; what happened? Wants detail, timestamps,
and stack traces. Read rarely, by someone technical, possibly the owner
pasting it into an issue, which is why it must contain no personal data.

**Accountability.** ALFRED acted on my life; what did it do and who asked?
Wants a durable, owner-readable record of every dispatched tool call,
including the denied ones. This is not a log, it is a feature, and it is
part of the security model.

## What good looks like

- Structured logging to a rotating local file, level configurable, default
  quiet enough that the terminal stays usable.
- **No owner content in log records.** Identifiers, not payloads.
- The audit trail is a first-class store collection, queryable by the owner
  through a command, and it records denials as prominently as approvals. A
  burst of denials is what an injection attempt looks like from inside.
- Bounded retention on both, automatic, so neither becomes an unbounded
  transcript of a life. See [[Privacy and Data Minimisation]].
- A `doctor` command that reports current state as a checklist: this is the
  local equivalent of a health endpoint, and it is what an owner actually
  uses.
- Errors surface to the owner in their transport, in one sentence, in
  addition to being logged.

## What bad looks like

- Logging the full prompt at debug level. It is the single most sensitive
  string in the process and it will end up in a pasted bug report.
- An audit record that stores tool arguments verbatim and forever, turning
  the control into the largest plaintext store in the system.
- `print()` for diagnostics, which cannot be filtered, redirected, or
  levelled.
- Silent failure paths with no record at all, which is the worst outcome
  for both audiences.

## How ALFRED does it

`alfred/logging_setup.py` configures logging centrally. The dispatcher
writes an audit record for every call, allowed or denied, into
`Collections.AUDIT`. The heartbeat sweeps messages and audit on a bounded
schedule. `alfred doctor` reports config, model reachability, agent load
results including skipped folders, and transport wiring. Terminal output
goes only through `runtime/cli.py` and `runtime/ui.py`, so nothing prints
from a library path.

## Verification

- `tests/test_dispatch.py` covers audit records on both the allow and deny
  paths.
- `tests/test_heartbeat.py` covers retention sweeps.
- ruff's `T20` rules ban `print` outside `scripts/`.

Open gaps: nothing asserts that a log record never contains owner content,
and audit records are not redacted by rule, only by convention. Both in
[[Gap Register]].

## Sources

- Charity Majors on observability as "can you ask a new question of your
  system", read here as "can the owner ask what it did".
- The Python `logging` documentation on library-versus-application
  configuration.
