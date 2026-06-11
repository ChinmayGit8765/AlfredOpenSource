# ALFRED

Self-hosted, local-first, multi-agent life-optimization system. Pure-domain
ports-and-adapters Python; the spec is `docs/SPEC.md`, the binding module
contracts are `ARCHITECTURE.md`. Read both before changing anything.

## Commands

- Test: `.venv\Scripts\python.exe -m pytest -q` (fully offline; no Ollama or
  Discord needed)
- Install: `py -3.13 -m uv pip install -e ".[dev,mcp]" --python .venv`
- Run (fake brain, no services): `.venv\Scripts\python.exe -m alfred.runtime.cli chat --fake`
- Run (real): `alfred chat` (needs Ollama), `alfred run` (needs Ollama +
  `ALFRED_DISCORD_TOKEN`)

## Hard rules

- `alfred/domain/*` never imports adapters, runtime, config, or I/O
  libraries. Time only via `ClockPort`. All effects via ports.
- Wiring happens only in `alfred/runtime/composition.py`.
- All structured LLM output goes through `domain/structured.structured_call`
  with a pydantic schema. Never parse raw model text ad hoc.
- Tool calls go through `domain/dispatch.ToolDispatcher` (allowlist +
  capability-tier gating + audit). Never invoke `ToolPort` directly from an
  agent path.
- Store collection names come from `schemas.Collections`, never bare strings.
- pydantic v2 idioms only; no `print` outside `runtime/cli.py`; comments say
  why, not what; no em-dashes in prose.
