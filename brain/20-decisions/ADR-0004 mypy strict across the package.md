---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0004: mypy strict across the package

## Status

Accepted.

## Context

The system is built on `Protocol` ports. Nothing inherits from
`StorePort`; nothing checks at runtime that `SqliteStoreAdapter` satisfies
it. The claim "this adapter implements this port" is checked by exactly one
thing, and if that thing is not run, it is checked by nothing.

Measured before deciding: `mypy --strict` on the whole package produced ten
errors across eight files. That is unusually clean, and it changed the
calculus entirely. Strict mode was affordable *today*; the usual argument
against it ("hundreds of errors, we will do it incrementally") did not
apply.

All ten were real:

- two functions with a bare `dict` return, so the contents were unchecked
- a `Literal` variable assigned a plain `str`
- two default-argument lambdas whose types could not be inferred
- a module-level dict with no annotation
- `_run(fn: Callable[[], Any]) -> Any` in the sqlite adapter, which erased
  the return type of every store operation in the system
- a loop variable shadowing an `except ... as` name
- a `list` comprehension over an untyped attribute leaking `Any`

None were cosmetic. Each was a place the code was less checked than it
appeared.

## Decision

`strict = true` for the whole `alfred` package, plus `warn_unreachable` and
`warn_unused_ignores`. Configuration in `pyproject.toml`, so `mypy` with no
arguments means the same thing in a shell and in CI.

`ignore_missing_imports` is scoped to `mcp.*` alone, because that package
is an optional extra whose absence must not turn a typecheck into an import
error. Everything else gets real stubs (`types-PyYAML`).

Every `type: ignore` carries an error code and a comment explaining why the
checker is wrong.

## Consequences

### What this buys

Protocol conformance is proven on every run. `Any` cannot leak in from an
untyped boundary and hollow out the strictness from the edges. Refactors
across the port boundary are mechanical.

### What this costs

`warn_unreachable` produces false positives on correct concurrency code.
The double-checked lock in the MCP adapter needs
`# type: ignore[unreachable]`, because mypy narrows the attribute from the
first check and cannot see a concurrent write. That is one suppression, and
it is annotated.

New code must be fully typed. On this codebase that is already the norm.

### What we gave up

Nothing that was being used.

## Alternatives considered

**Per-module strictness, tightened over time.** The standard incremental
approach. Rejected because the measurement showed it unnecessary: ten
errors is an afternoon, and "incremental" in practice means the interesting
old modules stay unchecked indefinitely.

**Drop `warn_unreachable`** to avoid the false positive. Rejected: it
catches real narrowing mistakes, and one annotated suppression is cheaper
than losing the rule.

## Verification

`mypy` in the `static` CI job and in pre-commit. Currently clean across 45
source files.
