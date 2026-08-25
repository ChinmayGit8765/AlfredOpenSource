---
tags: [standard, architecture]
status: enforced
applies-to: [alfred/domain, alfred/ports, alfred/adapters, alfred/runtime]
---

# Ports and Adapters

## What it is

Alistair Cockburn's hexagonal architecture. The application's logic sits in
the middle and knows nothing about the outside world. Everything the
outside world can do to it, and everything it can do to the outside world,
is expressed as a **port**: an interface owned by the inside. An
**adapter** implements a port using a real technology. A single
**composition root** is the one place that knows which adapter fills which
port.

The value is not the diagram. The value is one property: **all dependency
arrows point inward.** The domain can be read, tested, and reasoned about
without knowing that sqlite, Ollama, or Discord exist.

## Why it matters here

Three reasons specific to this project.

**The interesting logic is decision logic.** Whether two plans collide on a
Tuesday, whether a habit has lapsed twice, whether a tool is allowed: these
are the parts that must be right, and they are pure functions of state. If
they can only be exercised by starting a model server and a Discord
gateway, they will be under-tested, and the parts that decide whether to
act on someone's life will be the least verified code in the system.

**The backends are all temporary.** Ollama today, something else in two
years. Discord today, Telegram and HTTP already, Matrix eventually. Sqlite
today, maybe something with sync later. Each of those is a swap of one file
if the boundary held, and a rewrite if it did not.

**The security gate lives on the boundary.** Capability tiers, the
allowlist, and provenance checks only work if there is exactly one way to
reach a tool. That is a ports-and-adapters property before it is a security
property. See [[LLM Agent Safety]].

## What good looks like

- The domain imports the standard library, its schema validator, and its
  own ports. Nothing else.
- A port is a `Protocol`, defined by what the domain needs, not by what the
  adapter happens to offer. `StorePort` has `put/get/delete/append/query`
  because that is what the domain calls, not because sqlite has them.
- Adapters are dumb. Translation and I/O, no decisions.
- The composition root is boring, long, and the only file that imports both
  sides.
- Time, randomness, and identity generation are ports too. Anything that
  makes a function return different values on two identical inputs is a
  dependency, and dependencies get injected.

## What bad looks like

- `from alfred.adapters.sqlite_store import SqliteStoreAdapter` anywhere
  outside the composition root. It usually arrives as "just for a type
  hint", then as a default argument, then as a hard dependency.
- A port shaped like its adapter: `execute_sql()` instead of `query()`.
  This is the most common way the pattern degrades while still looking
  correct.
- `datetime.now()` in the domain. It reads as harmless and it makes the
  behaviour untestable at any date but today.
- A "utils" module that everything imports and that quietly grows I/O.

## How ALFRED does it

`alfred/domain/` is pure logic: the Conductor, the executor, the builder,
governance, memory, the roadmap. `alfred/ports/` holds five protocols:
`ModelPort`, `TransportPort`, `StorePort`, `ToolPort`, `ClockPort`.
`alfred/adapters/` holds the implementations. `alfred/runtime/composition.py`
is the single composition root, and its own docstring states the rule:
nothing outside it constructs an adapter.

`ClockPort` is the tell that the boundary is taken seriously. A codebase
that injects its clock has usually thought about the rest.

## Verification

`tests/test_architecture.py`, five tests, all reading the parsed AST:

- `test_domain_imports_stay_inside_the_allowlist`: an allowlist, not a
  denylist, so a new I/O library fails without anyone remembering to ban it.
- `test_ports_depend_on_nothing_inside_alfred_but_each_other`
- `test_domain_reads_time_only_through_the_clock_port`
- `test_only_the_composition_root_names_an_adapter`
- plus the two chokepoint tests described in [[LLM Agent Safety]].

Each was verified against a deliberate violation before landing. See
[[ADR-0001 Architecture rules are tests]].

## Sources

- Cockburn, *Hexagonal Architecture* (2005).
- Freeman and Pryce, *Growing Object-Oriented Software, Guided by Tests*,
  on ports as "what the application needs".
- Martin, *Clean Architecture*, for the dependency rule stated generally.
