---
tags: [finding]
severity: medium
status: fixed
found: 2026-08-25
---

# Finding 002: Four latent defects found while adding the gates

## What is wrong

Turning on `mypy --strict` and a curated ruff ruleset surfaced four defects
that were not style issues. Each is recorded because each is a shape worth
recognising again.

## 1. A naive sentinel in a timezone-aware sort

`alfred/runtime/core.py`, latest-plan selection:

```python
dated = [p for p in plans if p.created_at is not None]
return max(dated, key=lambda p: p.created_at or datetime.min)
```

`datetime.min` is naive. `created_at` is aware. Comparing them raises
`TypeError: can't compare offset-naive and offset-aware datetimes`.

The sentinel is unreachable today, because the list comprehension already
excluded every `None`, so `or datetime.min` never fires. It exists only to
satisfy the type checker, and it is a landmine: any future edit that widens
the filter turns a plan lookup into a crash in the middle of the owner's
week.

**Fix**: pair the timestamp with the plan, so no sentinel is needed.

```python
dated = [(p.created_at, p) for p in plans if p.created_at is not None]
return max(dated, key=lambda pair: pair[0])[1]
```

**Caught by**: ruff `DTZ901`. Related: `DTZ` as a whole is worth having in
a system that must never produce a naive datetime.

## 2. A generic helper that erased every caller's type

`alfred/adapters/sqlite_store.py`:

```python
async def _run(self, fn: Callable[[], Any]) -> Any:
```

Every store operation goes through `_run`. `query()` declares
`-> list[dict[str, Any]]` and returns `await self._run(work)`, which is
`Any`. So the declared return type was unverified, and the same erasure
applied to `get`, `delete`, and `append`.

This is the `Any`-contagion shape: one untyped helper at a boundary
hollows out strict typing across every call site, while everything still
typechecks.

**Fix**: make it genuinely generic. `async def _run[T](self, fn:
Callable[[], T]) -> T`.

**Caught by**: `mypy --strict`, `no-any-return`.

## 3. A `zip()` that would silently truncate the banner

`alfred/runtime/ui.py` zips five banner rows against five fade colours.
Without `strict=`, a future edit adding a sixth row prints five and drops
one, silently. A masthead missing its bottom row is exactly the kind of
thing nobody notices in a screenshot.

**Fix**: `zip(..., strict=True)`, with a comment: an editing slip should
crash, not truncate.

**Caught by**: ruff `B905`.

## 4. A one-second poll on the shutdown path

`alfred/runtime/cli.py`:

```python
async def watch_stop() -> None:
    while not system.core.stop_requested:
        await asyncio.sleep(1)
```

A busy wait, and up to a second of latency on `alfred stop`, on a flag that
another coroutine sets in the same event loop.

**Fix**: `AlfredCore` holds an `asyncio.Event`. `stop_requested` becomes a
property over it so the existing attribute contract is unchanged, and the
supervisor awaits `core.wait_for_stop()`.

**Caught by**: ruff `ASYNC110`.

## The pattern worth noting

None of these were found by reading. All four were found by a tool that
was not previously enabled, in a codebase that is otherwise unusually
clean: `mypy --strict` reported only ten errors across 45 modules, and
ruff's full curated ruleset only about a hundred, most of them test style.

That ratio is the argument for enabling the gates on a *good* codebase.
The four defects were sitting in the 2% the tools disagreed with, and no
amount of review had surfaced them.

## Status

All four fixed. See [[Typing Discipline]],
[[ADR-0004 mypy strict across the package]], and
[[Concurrency and Async Discipline]].
