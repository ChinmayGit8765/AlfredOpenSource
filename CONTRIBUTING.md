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
4. Tests run fully offline (`python -m pytest -q`, about 5 seconds). If
   your change needs Ollama or Discord to test, redesign it against the
   fakes in `alfred/testing/fakes.py`.

## Setup

```
uv venv
uv pip install -e ".[dev,mcp]"
python -m pytest -q
python -m alfred.runtime.cli chat --fake
```

## Good first contributions

- A new agent folder under `agents/` (manifest + prompt, no code).
- An MCP server config recipe in `config/mcp.example.yaml` with sane
  capability-tier classifications.
- Improvements to lapse diagnosis and the builder's elicitation prompts.

## Style

Python 3.12+, full type hints, pydantic v2 idioms, comments explain why
rather than what, and no em-dashes in prose. Match the code around you.
