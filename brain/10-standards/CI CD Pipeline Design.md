---
tags: [standard, operations, tooling]
status: enforced
applies-to: [.github/workflows]
---

# CI CD Pipeline Design

## What it is

How the automated checks are organised: what runs, where, in what order,
and how a failure communicates its own cause.

## Why it matters here

A single-maintainer project has one scarce resource, which is the
maintainer's attention. A pipeline that is slow, flaky, or vague spends
that resource and returns nothing. Worse, it trains the maintainer to merge
on red, at which point the pipeline is a cost with no benefit.

The specific mistake this project started with: **one job doing
everything**, run nine times across the platform and interpreter matrix.
A lint error and a Windows-only test failure looked identical in the check
list, and the lint error was paid for nine times.

## What good looks like

- **Jobs split by what they prove.** Platform-independent checks (lint,
  types) run once. Platform-dependent checks (the test suite) run across
  the matrix. The job name is the diagnosis.
- `fail-fast: false` on the matrix, so one platform failing does not hide
  whether the others also fail. Knowing "Windows only" versus "everywhere"
  is most of the debugging.
- Concurrency cancellation on a branch: a second push makes the first run's
  results irrelevant, and paying for them delays the relevant one.
- Least-privilege permissions by default, widened per job.
- **A job that tests the artefact, not the source.** Build the wheel,
  install it into a clean environment, run the entry point. This is the
  only check that catches a module missing from the package.
- Scheduled runs for anything time-sensitive (vulnerability audits), since
  a personal project can go a month without a push.
- Every gate runnable locally with the same command CI uses. Configuration
  in `pyproject.toml` rather than in the workflow file, so `mypy` means the
  same thing in both places.

## What bad looks like

- One monolithic job. The failure is "ci failed".
- A quality gate that only exists in the workflow YAML, so a contributor
  cannot reproduce it and finds out in review.
- Flaky tests left in with a retry wrapper. A retry is how a real
  intermittent bug becomes invisible.
- Coverage reported and never enforced, which is a number nobody reads.
- Actions on mutable tags with write permissions, which is a supply chain
  hole. See [[Supply Chain Security]].

## How ALFRED does it

Three jobs in `ci.yml`:

- `static`: ruff and mypy, once, on 3.13.
- `test`: the offline suite across ubuntu, windows and macos on 3.12 and
  3.13, with branch coverage and a separate step for the domain and ports
  floor so a regression there is named rather than buried, then the offline
  smoke command.
- `build`: `uv build`, install the wheel into a clean venv, run
  `alfred --help`.

`security.yml` runs pip-audit, CodeQL, and gitleaks per push and weekly.
Concurrency cancels superseded runs; permissions default to
`contents: read`.

## Verification

The pipeline is its own verification. The property worth restating: every
gate is runnable locally as `ruff check .`, `mypy`, `python -m pytest -q`,
with no arguments, because the configuration lives in `pyproject.toml`.

## Sources

- Forsgren, Humble, Kim, *Accelerate*, on lead time and change failure rate
  as the metrics a pipeline should optimise.
- GitHub Actions documentation on `permissions` and `concurrency`.
