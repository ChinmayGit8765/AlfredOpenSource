---
tags: [map, moc]
---

# Decisions MOC

Architecture decision records. Each one captures the forces at the moment
of the decision, so a future reader can tell an intentional trade-off from
an accident. Records are immutable once accepted; a change of mind is a new
record that supersedes the old one.

Format is Michael Nygard's, trimmed. Template: [[ADR]].

| # | Decision | Status |
|---|---|---|
| 0001 | [[ADR-0001 Architecture rules are tests]] | Accepted |
| 0002 | [[ADR-0002 Lint but do not auto-format]] | Accepted |
| 0003 | [[ADR-0003 Two coverage floors]] | Accepted |
| 0004 | [[ADR-0004 mypy strict across the package]] | Accepted |
| 0005 | [[ADR-0005 Anchor every gitignore pattern]] | Accepted |
| 0006 | [[ADR-0006 No telemetry, ever]] | Accepted |
| 0007 | [[ADR-0007 A dependency budget]] | Accepted |
| 0008 | [[ADR-0008 Fail closed on unclassified tools]] | Accepted |

## Decisions inherited from the spec

Three choices predate this vault and are recorded in `docs/SPEC.md` and
`ARCHITECTURE.md` rather than here. They are listed so nobody reopens them
by accident:

- **Write the orchestration from scratch, import the primitives.** No
  bespoke inference engine, websocket protocol, schema validator, or MCP
  client; the Conductor, the agent model, and the builder are all hand
  written. See "Prime guardrail" in `docs/SPEC.md`.
- **The agent is a folder, not a class.** A manifest plus a prompt, on
  disk, discovered at startup. Every front-end that creates an agent emits
  the same folder shape. See [[Prompt and Agent Design]].
- **Time is injected.** Even the clock is a port. See
  [[Concurrency and Async Discipline]].
