---
tags: [standard, architecture, data]
status: enforced
applies-to: [docs/SPEC.md]
---

# Local First Software

## What it is

The seven ideals from Kleppmann, van Hardenberg, Litt and McGranaghan's
2019 essay: fast, multi-device, offline, collaborative, long-lived, private
and secure by default, and user-controlled. Software where the local copy
is the primary copy and the network is an enhancement.

## Why it matters here

It is the thesis, not a feature. `docs/SPEC.md` states the bet plainly: the
most important AI in a person's life will not live in a corporate data
centre optimising for engagement. Every architectural decision either
serves that or undermines it.

Being explicit about *which* ideals the project is betting on matters,
because pretending to all seven produces a worse system than committing to
three.

## What good looks like

Of the seven, ALFRED commits hard to four:

| Ideal | Commitment |
|---|---|
| **No spinners** (fast) | Everything but the model call is local and immediate. Memory recall is keyword scoring in microseconds, not an embedding round trip. |
| **The network is optional** | The full pipeline runs with `--fake` and no services at all. The real system needs only a local model. |
| **Longevity** | Plain sqlite, JSON documents, agents as folders of Markdown and YAML. All of it readable in twenty years with no ALFRED. |
| **Privacy and user control** | The owner holds the keys, the data, and the hardware. See [[Privacy and Data Minimisation]]. |

Two it explicitly does not chase:

- **Seamless collaboration.** ALFRED serves one owner. CRDTs would be
  significant machinery for a use case the spec rules out. That is a
  decision, not an omission.
- **Multi-device sync.** Deferred. The store is behind `StorePort`, so a
  syncing adapter is a swap rather than a rewrite, and that is the whole
  payoff of the boundary.

And the one that is a design constraint rather than an ideal:
**the data outlives the software.** A folder of Markdown prompts and a
sqlite file of JSON documents is a format a person can read with a text
editor. That is the actual guarantee behind "your intelligence, your keys,
your machine".

## What bad looks like

- A "local-first" system that needs an account to start.
- Cloud fallback that quietly becomes the default path.
- A binary or proprietary storage format, which converts "local" into "on
  your disk, but only we can read it".
- Telemetry, which is the contradiction of the whole stance. See
  [[ADR-0006 No telemetry, ever]].

## How ALFRED does it

Sqlite for state, folders for agents, Ollama for the model, no accounts, no
sync service, no telemetry. The `--fake` mode exists so the system is
demonstrable and testable with nothing installed, which is also the
strongest possible statement that the network is optional.

## Verification

Structural rather than behavioural, and asserted where it can be: the
domain import allowlist means no domain module can open a socket, and the
adapters that can are wired only to the model backend and the transports
the owner configured. The offline suite passing with no services is the
running proof.

## Sources

- Kleppmann, van Hardenberg, Litt, McGranaghan, *Local-first software:
  you own your data, in spite of the cloud* (Ink & Switch, 2019).
