---
tags: [map, moc]
---

# Brain Home

The engineering standards ALFRED is built against, the decisions taken
under them, and what the repository actually does today.

ALFRED is a specific kind of project, and the standards that matter follow
from that. It is **self-hosted**, so there is no operator to page and no
staging environment. It is **local-first**, so the owner's data is the
product and it never leaves their machine. It is **multi-agent over an
LLM**, so non-determinism is a design input rather than a bug. And it
**acts on the world**, so a wrong decision is not a bad answer, it is a
deleted calendar or a sent message.

Every standard here is chosen because one of those four facts makes it
load-bearing.

## Start here

- [[Standards MOC]]: the standards, grouped.
- [[Decisions MOC]]: what was decided and why.
- [[Repository Audit]]: the scorecard. What holds today, measured.
- [[Gap Register]]: what does not hold yet, ranked.
- [[Threat Model]]: who attacks this and how.

## The four facts, and what each one forces

### Self-hosted: nobody is on call

There is no ops team. The owner is the operator, the operator is asleep at
3am, and a system that needs babysitting will simply be uninstalled. So:
failures must be loud at the boundary and safe in the middle
([[Error Handling and Failure Modes]]), configuration must be validated at
startup rather than at first use ([[Configuration Management]]), and the
install must work from a clean clone on the first try
([[Python Packaging and Distribution]]).

### Local-first: the data is the product

The owner's memories, plans, and lapses live in one sqlite file on their
machine. That file is the reason the project exists. So: no telemetry
([[Local First Software]]), durability and forward-compatible schemas
([[Data Durability and Migration]]), and secrets that never touch the
repository or the logs ([[Secrets and Credential Handling]]).

### Multi-agent over an LLM: the core is non-deterministic

A model can return anything, including well-formed nonsense and text
written by an attacker. So: every model output is parsed against a schema
([[Structured Output Contracts]]), every tool call passes one gate
([[LLM Agent Safety]]), and behaviour is tested against fakes rather than
against a live model ([[Testing Strategy]]).

### It acts: mistakes are not recoverable

Sending a message, writing a calendar event, and deleting a file are not
undoable by an apology. So: capability tiers with a human confirm on
anything destructive, provenance tracking so external text is never
authority, and an audit trail of everything that ran
([[LLM Agent Safety]], [[Observability for Local Systems]]).

## Playbooks

The recurring procedures, written down so they come out the same every
time and so the reasoning does not have to be rebuilt from scratch.

- [[Adding a Dependency]]: the four questions, and the reinstall.
- [[Adding an Agent]]: the folder shape, the two fields that matter, and
  the capacity headroom check.
- [[Wiring an MCP Connector]]: classify by behaviour, never by name.
- [[Release Checklist]]: what counts as a breaking change here.
- [[Incident Response]]: a leaked token, an action that should not have
  run, a corrupt store.

## The meta-standard

> A rule that lives only in prose is not a rule. It is a hope.

This is the one idea the rest of the vault leans on. Architecture documents
describe intent; code drifts from intent one reasonable-looking patch at a
time, and nobody notices until the property everyone assumed is quietly
gone. The counter is [[Executable Architecture Rules]]: turn each binding
rule into a test that reads the source and fails by name.

ALFRED's are in `tests/test_architecture.py`. See [[ADR-0001 Architecture rules are tests]].
