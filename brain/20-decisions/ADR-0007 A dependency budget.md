---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0007: A dependency budget

## Status

Accepted.

## Context

`CONTRIBUTING.md` already states the rule and, more importantly, the
reason: "a self-hosted tool people trust has to be auditable in an
afternoon".

That reason is doing real work. The target user reads the source before
running it. Five dependencies can be skimmed. Fifty cannot, and at fifty
the honest security posture is "I trust PyPI", which is exactly the posture
the project exists to avoid. See [[Supply Chain Security]].

`docs/SPEC.md` already draws the line for the big pieces: write the
orchestration, the agent model, the Conductor, the builder and the domain
logic; import model serving, schema validation, the Discord gateway,
persistence, and the MCP client. Never write a bespoke inference engine,
websocket protocol, schema validator, or MCP client.

What was missing is the rule for the small pieces, which is where
dependency lists actually grow: a date library, a retry decorator, a
coloured-output helper, a dotenv loader.

## Decision

A dependency is added only when all four hold:

1. **It replaces something we must not write ourselves**, per the spec's
   prime guardrail: a protocol, a parser, a cryptographic primitive, a
   gateway client.
2. **The equivalent hand-written code would be substantial**, not a helper
   function. A decorator is not a dependency-shaped problem.
3. **It is maintained and its own dependency tree is small.** A package
   with fifteen transitive dependencies costs fifteen, not one.
4. **The PR argues it**, and the argument goes in the description, where
   the PR template asks for it.

Development dependencies are held to (3) and (4) only. They do not ship,
but they do run in CI with repository access, which is its own threat
model.

Current runtime list, all five justified by the spec: `pydantic` (schema
validation), `pyyaml` (manifests), `ollama` (model serving), `discord.py`
(gateway), `rich` (terminal rendering). `mcp` is an optional extra.

## Consequences

### What this buys

The list stays skimmable, which is the security property. It also keeps
install friction low, which matters for software people try on a whim.

### What this costs

Some things get written by hand that a library would do. `chunk_text` for
Discord's 2000-character limit is 17 lines that a library would provide.
That is the trade being made deliberately.

### What we gave up

Convenience, sometimes. The rule will occasionally be annoying, which is
the sign that it is doing something.

## Alternatives considered

**No policy, judge case by case.** How every large dependency list starts.
Each addition is individually reasonable.

**A hard numeric cap.** Rejected as arbitrary: the right number depends on
what the dependencies are, and a cap invents pressure to vendor code
instead, which is worse.

## Verification

Not automatable, and stated as such. The check is social: `CONTRIBUTING.md`
carries the rule, the PR template has a checkbox for it, and `pip-audit`
in CI measures the cost of whatever gets through.
