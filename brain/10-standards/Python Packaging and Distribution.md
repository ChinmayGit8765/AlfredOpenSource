---
tags: [standard, tooling]
status: enforced
applies-to: [pyproject.toml, .github/workflows/ci.yml]
---

# Python Packaging and Distribution

## What it is

The modern Python packaging contract: a single declarative `pyproject.toml`
(PEP 621 metadata, PEP 517/518 build system), one build backend, and
optional dependency groups for the things not everyone needs.

## Why it matters here

The install is the first impression of a self-hosted tool, and the audience
is people who will read the source before they run it. An install that
needs a README workaround loses most of them before the system ever runs.

Two specific hazards:

**The editable-install lie.** `pip install -e .` puts the source directory
on the path, so a module missing from the wheel's package list still
imports fine on the developer's machine and fails for everyone else. Tests
pass locally and in CI, and the published artefact is broken.

**Optional extras that are not optional.** The MCP client is an extra. If a
non-extra module imports it at the top level, the base install breaks, and
the failure is an `ImportError` at startup rather than a clear message.

## What good looks like

- Everything in `pyproject.toml`: metadata, dependencies, entry points, and
  every tool's configuration. No `setup.py`, no `setup.cfg`, no `.flake8`,
  no `mypy.ini`. One file to read, and no tool that behaves differently in
  CI than in a shell because it found a different config.
- `requires-python` that matches reality, and classifiers that agree with
  it.
- Dependency floors (`>=`) for a library, not pins. Pins belong in a lock
  file for an application; a self-hosted tool people install alongside
  other things should not fight their environment.
- Optional extras for anything not needed by the default path, and code
  that imports them lazily or guards the import.
- **CI installs the built wheel into a clean environment and runs the
  console script.** This is the only check that catches the editable lie.

## What bad looks like

- Configuration scattered across five dotfiles that disagree.
- Exact pins in `dependencies` for an installable tool.
- A version number that lives in two places and drifts.
- Never building the sdist, then discovering at release time that it is
  missing half the package.

## How ALFRED does it

One `pyproject.toml`: PEP 621 metadata, hatchling as the backend, `mcp` and
`dev` extras, a single `alfred` console script, and the configuration for
pytest, coverage, ruff, and mypy all inline.

CI's `build` job runs `uv build`, installs the resulting wheel into a fresh
virtual environment, and executes `alfred --help`. That job would have
caught a missing subpackage or a broken entry point.

Open question, deliberately unresolved: the version lives only in
`pyproject.toml` while `alfred.__version__` is read at runtime for the
banner. See [[Release and Versioning]].

## Verification

The `build` job in `.github/workflows/ci.yml`.

## Sources

- PEP 517, PEP 518, PEP 621, PEP 660 (editable installs).
- The Python Packaging Authority's packaging guide.
