---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0001: Architecture rules are tests

## Status

Accepted.

## Context

`ARCHITECTURE.md` and `CLAUDE.md` state rules the system's safety depends
on: the domain does no I/O, time comes from a port, wiring happens in one
module, every structured model call goes through `structured_call`, every
tool call goes through `ToolDispatcher`, collection names come from
`Collections`.

All of them were prose. Prose is enforced by whoever is reviewing, on the
day, if they happen to think of it.

The failure mode is not a bad actor. It is a reasonable patch: a new
feature needs a bit of text from the model, so it calls
`ModelPort.complete` directly. It works. It reviews fine, because the diff
looks like every other diff. Schema validation and the repair retry now do
not apply to that path, and nothing anywhere goes red. The property is
gone, and the document still claims it.

Two forces made this urgent rather than theoretical. First, the rules here
are security controls in structural clothing: "one path to the tool port"
is what makes the allowlist, the capability gate, and the audit trail true.
Second, plausible-looking patches are now cheap to generate in volume, and
review is much better at catching wrong answers than at catching a correct
change that crosses a boundary.

## Decision

Every binding rule in `ARCHITECTURE.md` and `CLAUDE.md` is asserted by a
test in `tests/test_architecture.py` that parses the source with `ast` and
fails with a file and a line.

Rules:

- Guards **read** source; they never import the modules they judge.
- Guards are allowlist-shaped where possible, so an unforeseen violation
  fails by default.
- The failure message says what to do instead, not just that something is
  wrong.
- Every guard is verified against a deliberate violation before it is
  trusted.
- A rule that needs a third exception is a wrong rule and gets rewritten,
  not exempted.

## Consequences

### What this buys

The rules are true on every run, on every platform, for every contributor,
including the ones who never read `ARCHITECTURE.md`. Onboarding gets
cheaper: a newcomer who crosses a boundary is told immediately, by name,
with the fix in the message.

The documents become trustworthy, because they can no longer silently
diverge from the code.

### What this costs

Seven more tests to maintain. An AST guard is slightly awkward to read.
When a rule legitimately needs to change, two things change instead of one.

### What we gave up

Some flexibility. A genuinely justified exception now needs an explicit
allowlist entry with a comment, which is friction. That friction is the
point, but it is a real cost on the day someone hits it for a good reason.

## Alternatives considered

**Rely on review.** The status quo, and the thing that failed. Review
catches what the reviewer thinks to check.

**`import-linter`.** A real tool, and a reasonable choice for the import
rules. Rejected because it covers only imports: it cannot express "one
caller of `.complete()`", "no bare collection strings", or "time only
through the clock", which are four of the seven guards. Adding a dependency
that covers 40% of the need failed [[ADR-0007 A dependency budget]].

**A custom linter plugin.** More machinery, less readable, and it lives
outside the test suite, so a contributor running `pytest` would not see it.

## Verification

The guards are self-verifying by construction: introduce the violation,
watch the named test fail. Done for all seven. If a guard ever stops being
able to fail, it is decoration and should be deleted.
