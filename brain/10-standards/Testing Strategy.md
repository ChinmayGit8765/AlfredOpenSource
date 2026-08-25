---
tags: [standard, testing]
status: enforced
applies-to: [tests, alfred/testing/fakes.py]
---

# Testing Strategy

## What it is

What gets tested, at what level, against what substitutes for the real
world, and how fast the whole thing runs.

## Why it matters here

The dependencies are a local LLM, a Discord gateway, and MCP subprocesses.
Every one of them is slow, non-deterministic, or unavailable on a
contributor's machine. If the test suite needs any of them, three things
follow: contributors do not run it, CI needs secrets, and the tests that do
exist assert almost nothing because the model's output varies.

So the requirement is stronger than "have tests": **the whole suite runs
offline, in seconds, on a clean clone, with no services and no keys.**
Everything else follows from that constraint.

The other reason is what this system does. A bug in a planning system is a
wrong plan, which is annoying. A bug in the governance path is a
destructive tool that ran without asking, which is not.

## What good looks like

- **Fakes, not mocks.** A `FakeModel` that returns a scripted queue of
  responses and records the messages it was given, a `MemoryStore`
  implementing the real `StorePort`, a `CapturingTransport` that keeps what
  was sent. Mocks assert that code called a method; fakes let you assert
  what the system *decided*, which is the thing you actually care about.
  Mock-heavy suites also lock in the implementation, so every refactor
  breaks tests without any behaviour changing.
- **A controllable clock.** Injected via `ClockPort`, so "two consecutive
  misses moves the agent to lapsing" is a test, not a two-week wait.
- **Truth tables for policy.** Where the spec has a table (tier by
  provenance by setting), the test is that table, parametrized, with every
  cell present. Nobody can add a row to the code and forget a case.
- **Tests that fail for one reason.** A failure message that names the
  property, not `assert result == expected`.
- **Branch coverage with a floor**, differentiated by layer. Pure decision
  logic has no excuse for an untested branch; a terminal rendering function
  does.
- Warnings as errors, so a dependency's deprecation is caught the week it
  appears rather than the release it breaks.

## What bad looks like

- `@patch("alfred.domain.executor.something")`. If a test needs to patch
  inside the domain, the dependency was not injected, and the design is
  wrong before the test is.
- A test suite that skips half its cases when a service is absent, and
  therefore reports green while checking nothing.
- Coverage as the only quality signal. 100% coverage of code that asserts
  nothing meaningful is a slower way to have no tests.
- Golden-file assertions against model prose, which fail on every model
  update for no real reason.

## How ALFRED does it

575 tests, about seven seconds, fully offline. `alfred/testing/fakes.py`
provides `FakeModel`, `MemoryStore`, `CapturingTransport`, and a fake clock.
The governance truth table is a parametrized test over every tier and
provenance combination. `--fake` mode runs the entire pipeline with a
`DryRunModel`, so `demo-roundtrip --fake` is both a smoke test and a
contributor's first run.

Coverage floors are split: the package as a whole and, higher, the domain
and ports. See [[ADR-0003 Two coverage floors]].

## Verification

`python -m pytest -q` locally; the matrix job in CI across three operating
systems and two interpreters, plus the domain and ports floor as its own
step so a regression there is named in the job list.

Not yet present: mutation testing, and property-based tests for the
conductor's conflict detection, which is the one place where hand-written
cases are most likely to miss a shape. See [[Gap Register]].

## Sources

- Meszaros, *xUnit Test Patterns*, for the fake versus mock distinction.
- Fowler, *Mocks Aren't Stubs*.
- Hillel Wayne on property-based testing for combinatorial logic.
