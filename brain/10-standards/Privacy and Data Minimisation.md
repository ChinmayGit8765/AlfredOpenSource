---
tags: [standard, security, data]
status: enforced
applies-to: [alfred/domain/memory.py, alfred/adapters/sqlite_store.py]
---

# Privacy and Data Minimisation

## What it is

Collecting only what the system needs, keeping it only as long as it is
useful, and making sure the owner can see and delete any of it.

## Why it matters here

ALFRED's data is not "user data" in the analytics sense. It is a person's
injuries, exam anxieties, therapy notes, and the things they keep failing
at. The GDPR framing is a useful lens even though a self-hosted personal
tool is outside its scope: purpose limitation, storage limitation, and the
right to erasure are good engineering here for reasons that have nothing
to do with law.

The privacy risk in a local-first system is not exfiltration by the vendor,
because there is no vendor. It is:

- **The log file.** Personal data written to a log the owner then shares.
- **The prompt.** Everything the system knows, assembled and sent to
  whatever model backend is configured. If that backend is remote, the
  privacy story changes entirely.
- **Unbounded retention.** A message log that grows forever is a
  transcript of someone's life sitting in a file they forgot about.

## What good looks like

- **No telemetry.** Not opt-out, not anonymised, none. See
  [[ADR-0006 No telemetry, ever]].
- Logs carry identifiers, never content. A row that failed to parse is
  named by key.
- Retention sweeps with a real bound, running automatically, on the
  collections that grow without limit.
- Owner-facing commands to see and delete: `memories`, `forget <id>`.
- The prompt assembles only what is *relevant* to the message at hand, not
  the whole store. This is a privacy property before it is a token-budget
  one.
- A local model by default, and an explicit, informed choice to point at a
  remote one.

## What bad looks like

- "Anonymous usage statistics to help us improve." For a system holding
  this data, the mere pattern of use is sensitive.
- A crash reporter.
- An audit log that records tool arguments verbatim forever, which turns
  the security control into the largest plaintext store in the system.
- Growing the prompt to "give the model more context", which is a quiet
  decision to send more of someone's life to a backend.

## How ALFRED does it

Nothing phones home. The heartbeat runs bounded retention sweeps over the
messages and audit collections, at most daily, deleting in capped batches
so one sweep can never stall a tick. Memory recall is deterministic
keyword scoring with a recency bonus rather than embeddings, chosen partly
because it runs offline with no model call, which means recall never sends
anything anywhere.

`_decode_doc` logs a skipped row by key with a comment stating that rows
can hold personal data. `structured_call` does not log raw model output.

## Verification

- `tests/test_heartbeat.py` covers the retention sweep bounds.
- `tests/test_memory.py` covers `forget`.
- The no-telemetry property is verified by the domain import allowlist:
  nothing in the domain can import an HTTP library, and the adapters that
  can are wired only to the model backend and the configured transports.

Open gap: no test asserts that the assembled prompt excludes memories
irrelevant to the current message, which is the property that keeps the
prompt small. See [[Gap Register]].

## Sources

- GDPR articles 5 (principles) and 17 (erasure), as design guidance rather
  than compliance.
- The `local-first` literature on data ownership, see
  [[Local First Software]].
