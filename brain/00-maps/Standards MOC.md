---
tags: [map, moc]
---

# Standards MOC

Every note answers the same four questions: what the standard is, why it
matters *for a system like this one*, what good and bad look like, and how
to verify it. The verification line is the point. See [[Brain Home]].

## Architecture

- [[Ports and Adapters]]: the shape, and the two rules that make it real.
- [[Executable Architecture Rules]]: fitness functions, or why the previous
  note is worthless without tests.
- [[Concurrency and Async Discipline]]: one owner, one event loop, and the
  three ways that still goes wrong.
- [[Error Handling and Failure Modes]]: what a personal system owes its
  owner when something breaks.

## Language and tooling

- [[Typing Discipline]]: strict from the start is cheap; strict later is not.
- [[Python Packaging and Distribution]]: PEP 621, one build backend, and
  proving the wheel works.
- [[Configuration Management]]: precedence, validation, and the startup
  contract.

## Correctness

- [[Testing Strategy]]: fakes over mocks, offline always, and what the
  five-second suite buys.
- [[Structured Output Contracts]]: never parse model prose.
- [[Evaluating LLM Behaviour]]: how to test something non-deterministic
  without a live model.

## Security

- [[LLM Agent Safety]]: prompt injection, capability tiers, and the
  confirm gate.
- [[Supply Chain Security]]: the dependency budget, auditing, and what
  SLSA actually asks for.
- [[Secrets and Credential Handling]]: environment, never repository.
- [[Privacy and Data Minimisation]]: what a system that knows everything
  should refuse to collect.

## Data

- [[Data Durability and Migration]]: sqlite done properly, and schemas that
  outlive their writers.
- [[Local First Software]]: the seven ideals, and which three ALFRED is
  actually betting on.

## Operations

- [[CI CD Pipeline Design]]: fast signal, honest signal, and job splitting.
- [[Observability for Local Systems]]: logs and audit trails with no
  telemetry and no dashboard.
- [[Release and Versioning]]: semver where the contract is the API.

## People

- [[Documentation Standards]]: Diataxis, ADRs, and spec-as-contract.
- [[Open Source Project Hygiene]]: the files a stranger looks for first.
- [[Prompt and Agent Design]]: prompts are source code and deserve review.
- [[Accessibility and Terminal UX]]: the console is the product surface.
