---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0002: Lint but do not auto-format

## Status

Accepted.

## Context

The reflex when hardening a Python project is to add a formatter and run it
over everything. `ruff format` on this codebase reports 47 of 77 files
would be reformatted.

Those 47 files are not badly formatted. They are formatted by hand to a
narrower line width than the formatter's default, with line breaks chosen
to keep clauses together, and with wrapped prose comments that explain why
a decision was made. A formatter has no opinion about meaning; it has an
opinion about columns.

There is a concrete example. `_STOPWORDS` in `alfred/domain/memory.py` is a
triple-quoted block of words on three readable lines, `.split()` at the
end. An autofix rewrote it as a single 300-character list literal. Every
character was correct. It was worse.

Against that: a formatter genuinely does remove a class of review comment,
and it makes diffs smaller by eliminating incidental whitespace churn.

## Decision

Lint with ruff. Do not enforce formatting.

`ruff check` runs in CI and in pre-commit with a curated ruleset that
targets bug shapes (bugbear, async correctness, naive datetimes, logging
misuse, pytest style) rather than layout. `E501` is ignored: line length is
a formatter's concern, and there is no formatter.

`ruff format` is not run, not checked, and not in pre-commit.

## Consequences

### What this buys

The existing formatting, which is deliberate and readable, survives. The
hardening diff stayed reviewable: it touched what needed to change instead
of every file in the repository. Comments that explain reasoning keep their
shape.

### What this costs

Formatting is now a review concern. On a project with many contributors
that is a real tax, and this decision would be wrong there.

New contributors have no mechanical answer to "how should this be
formatted" beyond "match the code around you", which `CONTRIBUTING.md` says
explicitly.

### What we gave up

The whitespace-churn-free diffs a formatter gives you.

## Alternatives considered

**Format everything now.** Rejected: a several-thousand-line diff with zero
behaviour change, landing on top of real fixes, which makes the real fixes
unreviewable.

**Format only new files.** The worst option. Two styles in one codebase,
and the boundary is invisible.

**Adopt the formatter and configure it to match.** Investigated; the hand
formatting is not expressible as a configuration, because the choices are
semantic rather than mechanical.

## Verification

`ruff check .` is clean and enforced. The absence of a formatter is
verified by its absence from `.pre-commit-config.yaml` and `ci.yml`, both
of which carry a comment saying it is deliberate, so nobody adds it back as
an oversight.

Revisit if this project gains regular contributors. At that point the
trade-off inverts.
