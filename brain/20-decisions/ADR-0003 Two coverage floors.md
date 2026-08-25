---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0003: Two coverage floors

## Status

Accepted.

## Context

Branch coverage measured at the start of this work: 80% for the package,
94% for `alfred/domain` and `alfred/ports` together, and 24% for
`alfred/runtime/cli.py`.

A single package-wide floor forces a choice between two bad options. Set it
at 80 and the domain, where the decision logic that must be right lives,
is free to rot to 80 while the CLI is written up. Set it at 94 and the
gate fails until several hundred lines of argument parsing and terminal
rendering are tested, which produces low-value tests written to satisfy a
number.

The layers genuinely differ. `alfred/domain/conductor.py` decides whether
two plans collide on a Tuesday: pure, total, and there is no excuse for an
untested branch. `alfred/runtime/cli.py` parses arguments and prints
tables: exercised by hand constantly, and a test asserting that a table has
a "shape" column is not worth its maintenance.

## Decision

Two floors, enforced separately.

- **Package**: `fail_under = 79` in `[tool.coverage.report]`. A backstop,
  not a target.
- **Domain and ports**: 93%, enforced as its own CI step
  (`coverage report --include="alfred/domain/*,alfred/ports/*"
  --fail-under=93`).

Both use branch coverage, not line coverage. Both sit roughly one point
below the measured value, so an unrelated refactor does not fail
spuriously while a real regression does.

The second check is a separate CI step so that a regression in the domain
appears as its own named failure rather than a slightly lower number in a
job that also does five other things.

## Consequences

### What this buys

The layer where correctness matters is held to a standard the whole package
could not reach, and the standard is legible: "pure logic has no excuse for
an untested branch".

### What this costs

Two numbers to maintain, and a second CI step. The floors need raising as
coverage improves, or they stop being a ratchet, and nothing automates
that.

### What we gave up

The simplicity of one number.

## Alternatives considered

**One floor at 80.** Lets the domain regress silently. The whole point is
that the domain is different.

**Per-file thresholds.** Coverage.py supports it. Rejected as too many
knobs: a per-file map is a thing nobody updates, and it obscures the actual
rule, which is about layers.

**No enforcement, report only.** A number in a log that nobody reads is not
a gate.

## Verification

Both floors run in CI on every matrix cell. The domain floor is a named
step, `Domain and ports coverage floor`, so its failure is self-describing.
