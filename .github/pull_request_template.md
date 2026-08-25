# What this changes

<!-- One paragraph. What the system does after this that it did not before. -->

## Why

<!-- The problem, not the patch. Link an issue if there is one. -->

## How it was verified

<!-- What you actually ran, and what it said. "Should work" is not a check. -->

- [ ] `python -m pytest -q` passes, fully offline
- [ ] `ruff check .` is clean
- [ ] `mypy` is clean
- [ ] New behaviour is covered by a test that fails without the change

## Contracts

The rules in `ARCHITECTURE.md` are binding and enforced by
`tests/test_architecture.py`. Confirm the ones this change touches:

- [ ] `alfred/domain/*` imports no adapter, runtime, config, or I/O library,
      and takes time only from `ClockPort`
- [ ] Any new wiring happens only in `alfred/runtime/composition.py`
- [ ] Every structured model call goes through `structured_call` with a
      pydantic schema
- [ ] Every tool call goes through `ToolDispatcher`
- [ ] Collection names come from `schemas.Collections`, never bare strings

## Risk

<!--
What breaks if this is wrong, and how would the owner notice? If it touches
governance, tool dispatch, or provenance, say explicitly what an attacker
or a prompt injection can and cannot do after this change.
-->

## Dependencies

- [ ] No new dependency, or: the reason it earns its place is below
