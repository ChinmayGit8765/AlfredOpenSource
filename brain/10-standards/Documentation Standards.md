---
tags: [standard, people]
status: enforced
applies-to: [docs, ARCHITECTURE.md, README.md]
---

# Documentation Standards

## What it is

Documentation organised by what the reader is trying to do, plus a
distinction between documents that *describe* the system and documents that
*bind* it.

## Why it matters here

Two reasons specific to this project.

**Some of these documents are contracts.** `ARCHITECTURE.md` does not
describe the module boundaries, it defines them, and code review audits
against it. `docs/GOVERNANCE.md` does not describe the capability gate, it
specifies it, and the truth table in it is implemented literally. Mixing
those in with a tutorial invites someone to "improve" a binding sentence.

**A self-hosted tool is read before it is run.** The audience is people
deciding whether to trust software with their whole life. They read the
architecture and the security model before they read the quickstart, which
is the reverse of most projects, and the docs should be organised for it.

## What good looks like

Diataxis, four modes, not mixed in one page:

| Mode | Answers | Here |
|---|---|---|
| Tutorial | "get me started" | README quickstart, `--fake` mode |
| How-to | "do this specific thing" | [[Adding an Agent]], [[Wiring an MCP Connector]] |
| Reference | "what are the fields" | `docs/AGENTS.md`, `docs/MODELS.md` |
| Explanation | "why is it like this" | `docs/SPEC.md`, `ARCHITECTURE.md`, this vault |

Plus:

- **Binding documents marked as binding**, in their own first paragraph.
- Decisions captured as ADRs, immutable once accepted, superseded rather
  than edited, so a reader can tell a considered trade-off from an
  accident. See [[Decisions MOC]].
- Code comments say **why**, not what. The what is in the code; the why is
  the thing that gets lost.
- Examples that are executable, so they cannot rot silently.

## What bad looks like

- A README that is tutorial, reference, and manifesto at once, so nobody
  can find anything.
- Comments restating the line above them.
- An architecture document that has drifted from the code, which is worse
  than no document because it is trusted.
- Decisions recorded only in a merged PR thread.

## How ALFRED does it

`docs/SPEC.md` is the binding product spec and says so in its first line.
`ARCHITECTURE.md` holds the module contracts. `docs/GOVERNANCE.md` is the
operator's view of the gate. `docs/AGENTS.md`, `docs/MODELS.md` and
`docs/CONNECTORS.md` are reference. The README is the tutorial and the
pitch. `CLAUDE.md` is the working agreement for anyone, human or model,
editing the code.

This vault adds the missing fourth mode: the *why behind the why*, which
standards were chosen, and what the repository actually scores against
them.

The comment convention ("why, not what") is in `CLAUDE.md` and visible
throughout: the `emits_plans: false` comment in the qa manifest explains
what the flag actually enforces rather than restating the field name.

## Verification

Documentation correctness is not automatically verifiable, which is worth
stating rather than pretending. What *is* verified:

- The example agents load, and their manifests match their folder names
  (`tests/test_repo_hygiene.py`).
- The executable examples run (`demo-roundtrip --fake` in CI).
- This vault's internal links all resolve (`tests/test_brain_vault.py`).

## Sources

- Procida, *Diataxis*.
- Nygard, *Documenting Architecture Decisions* (2011).
