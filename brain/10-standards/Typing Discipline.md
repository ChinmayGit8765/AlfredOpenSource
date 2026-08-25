---
tags: [standard, tooling]
status: enforced
applies-to: [alfred]
---

# Typing Discipline

## What it is

Static typing as a build gate rather than documentation. `mypy --strict`
means no untyped definitions, no implicit `Any`, no untyped decorators, and
no silently ignored imports.

## Why it matters here

A system built on protocols gets most of its safety from the type checker.
`StorePort`, `ModelPort`, and `ToolPort` are structural: nothing inherits
from them, and nothing at runtime checks that an adapter satisfies one. The
only thing standing between "this adapter implements the port" and a
runtime `AttributeError` in the middle of the owner's week is mypy.

The second reason is `Any` contagion. A single untyped boundary function
returning `Any` propagates through every caller, and strict mode's value
quietly evaporates from the edges inward. The sqlite adapter had exactly
this: `_run(fn: Callable[[], Any]) -> Any` erased the return type of every
store operation in the system. It typechecked. It was also lying about
five call sites.

## What good looks like

- `strict = true` for the whole package, not per-module opt-in.
- `warn_unreachable` and `warn_unused_ignores` on. The first catches
  narrowing mistakes; the second stops suppressions outliving their cause.
- Generic helpers are actually generic. PEP 695 syntax (`def f[T](...)`)
  where the project's minimum version allows it, which is cleaner than a
  module-level `TypeVar` used exactly once.
- Every `type: ignore` is **specific** (`[unreachable]`, not bare) and
  carries a comment explaining why the checker is wrong. If you cannot
  write that comment, the checker is right.
- Third-party stubs installed (`types-PyYAML`) rather than
  `ignore_missing_imports` blanket-applied.

## What bad looks like

- `ignore_missing_imports = true` globally. It hides real typos in import
  paths alongside the missing stubs.
- `Any` in a port signature. The port is the contract; `Any` in a contract
  is no contract.
- `# type: ignore` with no code and no comment.
- Strict mode enabled on new files only, which means the interesting old
  files stay unchecked forever.

## How ALFRED does it

`mypy --strict` is clean across all 45 modules with `warn_unreachable` and
`warn_unused_ignores` on. `ignore_missing_imports` is scoped to `mcp.*`
alone, because that package is an optional extra and its absence must not
turn a typecheck into an import error.

There is exactly one `type: ignore` in the codebase, on the second check of
a double-checked lock in the MCP adapter, where mypy's narrowing cannot see
a concurrent write. It is coded and commented.

Getting here took ten fixes, all of them real: two missing generic
parameters, a `Literal` assigned a `str`, two lambdas whose types could not
be inferred, a `dict` with no annotation, and the `Any`-erasing `_run`.
None were cosmetic; each was a place the code was less checked than it
looked.

## Verification

`mypy` in the `static` CI job, and in pre-commit. Configuration lives in
`pyproject.toml` so the command takes no arguments and cannot drift between
a developer's shell and CI.

## Sources

- PEP 544 (protocols), PEP 695 (type parameter syntax).
- The mypy documentation on strict mode and on `Any` propagation.
