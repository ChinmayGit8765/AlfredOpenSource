---
tags: [playbook]
---

# Adding a Dependency

The rule and its reasoning are in [[ADR-0007 A dependency budget]]. This is
the procedure.

## Before you add it

Answer all four, in the PR description:

1. **Does it replace something we must not write ourselves?** The spec's
   prime guardrail lists these: protocols, parsers, schema validators,
   cryptographic primitives, gateway clients. A retry decorator is not one.
2. **Would the hand-written equivalent be substantial?** If the answer is
   "about thirty lines", write the thirty lines.
3. **What does it drag in?** Check the transitive tree. A package with
   fifteen dependencies costs fifteen.
   ```
   uv pip install --dry-run <package>
   ```
4. **Is it maintained?** Last release, open issue count, whether a single
   unresponsive maintainer is the whole project.

If any answer is weak, the answer is no.

## Adding it

1. Add to the right group in `pyproject.toml`:
   - `dependencies` if the default path needs it
   - `[project.optional-dependencies] mcp` if only the MCP path does
   - `dev` if it never ships
2. Use a floor (`>=`), not a pin. This is installable software and it
   should not fight the user's environment.
3. If it is optional, **guard the import**. A top-level import of an extra
   in a non-extra module breaks the base install, and the failure is an
   `ImportError` at startup rather than a message.
4. Reinstall and re-run everything:
   ```
   uv pip install -e ".[dev,mcp]" --python .venv
   .venv/bin/python -m pytest -q
   ruff check . && .venv/bin/python -m mypy
   ```
5. If it lacks type stubs, add them (`types-*`) rather than reaching for
   `ignore_missing_imports`. Blanket ignoring hides real typos in import
   paths.

## After

- `pip-audit` runs in CI on the resolved tree. Watch that job on the PR.
- Dependabot will start proposing updates. Patch updates are grouped;
  anything else arrives on its own.
- If the base install is now heavier, say so in the PR. Install weight is a
  user-facing property for self-hosted software.

## Removing one

Cheaper than adding, and worth doing when a dependency is down to one call
site. Delete it from `pyproject.toml`, reinstall into a **fresh** venv
rather than the existing one, and run the suite: a stale environment will
happily keep importing something no longer declared.
