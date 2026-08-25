# Contributing to ALFRED

Glad you are here. ALFRED is small on purpose; contributions that keep it
small are the best kind.

## Ground rules

1. Read `ARCHITECTURE.md` first. The module contracts in it are binding:
   the domain layer does no I/O, all effects go through ports, wiring
   happens only in `alfred/runtime/composition.py`.
2. Every structured LLM output goes through `structured_call`. Every tool
   call goes through the dispatcher. No exceptions, including in tests.
3. New dependencies need a reason. The current list is short because a
   self-hosted tool people trust has to be auditable in an afternoon.
4. Tests run fully offline (`python -m pytest -q`, about 7 seconds). If
   your change needs Ollama or Discord to test, redesign it against the
   fakes in `alfred/testing/fakes.py`.
5. The rules in (1) and (2) are not on the honour system. They are asserted
   by `tests/test_architecture.py`, which parses the source and fails with
   a file and a line. If a guard blocks something you believe is correct,
   argue it in the PR rather than adding an exception.

## Setup

```
uv venv
uv pip install -e ".[dev,mcp]"
python -m pytest -q
python -m alfred.runtime.cli chat --fake
```

Optionally, run the same gates CI does before you push:

```
pre-commit install     # once
ruff check .
mypy
python -m pytest -q
```

There is deliberately no formatter; see `brain/20-decisions/`. Match the
code around you.

## Good first contributions

- A new agent folder under `agents/` (manifest + prompt, no code).
- An MCP server config recipe in `config/mcp.example.yaml` with sane
  capability-tier classifications.
- Improvements to lapse diagnosis and the builder's elicitation prompts.

## Where the reasoning lives

`ARCHITECTURE.md` and `docs/SPEC.md` are binding. `brain/` is an Obsidian
vault holding *why* those contracts are what they are: the standards
researched, the decisions recorded as ADRs, and an honest audit of where
this repository falls short. If you are proposing a change to how the
project is built rather than to what it does, read
`brain/00-maps/Brain Home.md` first, and add an ADR alongside your change.

## Style

Python 3.12+, full type hints, pydantic v2 idioms, comments explain why
rather than what, and no em-dashes in prose. Match the code around you.
