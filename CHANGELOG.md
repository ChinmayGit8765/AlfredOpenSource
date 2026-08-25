# Changelog

Notable changes to ALFRED. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0, the contracts in `ARCHITECTURE.md` are the API. A change to a
port signature, the capability-tier truth table, or the agent manifest
schema is a breaking change and is called one here, whatever the version
number says.

## [Unreleased]

### Fixed

- The shipped `build` agent is back in the repository. An unanchored
  `build/` pattern in `.gitignore`, meant for Python build artefacts, also
  matched `agents/build/`, so the agent existed in the working copy and in
  no clone of it. The loader found four agents where the tests, the README,
  and `docs/AGENTS.md` all expect five. Build-artefact patterns are now
  anchored to the repository root.
- `_latest_plan` sorted with a naive `datetime.min` sentinel, which would
  have raised rather than sorted had it ever been reached against an
  aware timestamp.
- The masthead `zip()` is now `strict`: a banner and a colour fade of
  different lengths fails loudly instead of silently printing fewer rows.
- `SqliteStoreAdapter._run` is generic, so it no longer erases every call
  site's return type to `Any`.

### Changed

- The service supervisor awaits the shutdown event instead of polling the
  stop flag once a second, so `alfred stop` takes effect immediately.
- The `build` agent claims 4 capacity points rather than 6. The shipped
  fleet now leaves room for the builder to add an agent on a fresh install
  instead of spending the default weekly budget on arrival.

### Added

- `tests/test_architecture.py`: the binding rules in `ARCHITECTURE.md` and
  `CLAUDE.md` are now asserted against the parsed source rather than left
  to review. Domain purity, time only through `ClockPort`, adapters named
  only in the composition root, `structured_call` as the single path to the
  model, `ToolDispatcher` as the single path to a tool, and no bare
  collection strings.
- `tests/test_repo_hygiene.py`: no source file is invisible to git, every
  agent folder ships with a manifest and a prompt, and the shipped fleet
  leaves capacity for the builder.
- Lint (`ruff`), types (`mypy --strict`, clean across all 45 modules), and
  branch coverage floors, all enforced in CI, plus a job that installs the
  built wheel into a clean environment and runs the console script.
- Supply-chain and security workflows: `pip-audit`, CodeQL with the
  `security-and-quality` queries, a full-history secret scan, and weekly
  Dependabot updates.
- `SECURITY.md`, `CODEOWNERS`, issue and pull request templates, and a
  `pre-commit` config that runs the same gates as CI.
- `brain/`: an Obsidian vault holding the standards research behind these
  decisions, the architecture decision records, and a live audit of the
  repository against each standard.

## [0.1.0]

First public shape of the system: the orchestration core, five hand-written
agents, the Conductor, the Adaptive Agent Builder, the governance gate, the
roadmap and wins ledger, and the offline test suite.
