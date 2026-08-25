---
tags: [standard, architecture, testing]
status: enforced
applies-to: [tests/test_architecture.py]
---

# Executable Architecture Rules

## What it is

An architectural fitness function: an automated test that asserts a
structural property of the codebase rather than a behaviour of it. Neal
Ford's term, from *Building Evolutionary Architectures*. Instead of "the
domain must not depend on adapters" living in a document, it lives in a
test that parses every domain module and fails with a file and a line.

## Why it matters here

Every architectural rule in this project is a security rule wearing
different clothes.

"All tool calls go through the dispatcher" sounds like tidiness. It is
actually the sentence that makes the capability tier gate, the per-agent
allowlist, and the audit trail true. The day one code path calls
`ToolPort.invoke` directly, all three silently stop applying to that path,
and nothing fails. No test goes red. The system still works, still passes
review, and has quietly lost the property it was built around.

That is the failure mode fitness functions exist for: **silent erosion of
an invariant nobody thought to test because it felt structural rather than
behavioural.**

A second reason, specific to LLM-assisted development: a plausible-looking
patch is now cheap to produce, in volume, by contributors and by models.
Review catches wrong answers. It is much worse at catching a correct-looking
change that crosses a boundary, because nothing in the diff looks wrong.
The test is what catches that.

## What good looks like

- The rule fails with a **file and a line**, not "architecture violated".
  A guard that cannot point at the offending line will be deleted the first
  time it is inconvenient.
- The failure message says **what to do instead**: "route the effect
  through a port", "go through `structured_call` with a pydantic schema".
- The rule is **allowlist-shaped** where possible. A denylist protects
  against the libraries you thought of.
- The guard **reads source, it does not import it**. Importing modules to
  inspect them turns a rule violation into an import error, and executes
  code you are trying to judge.
- Each guard is **proven to fail** at least once, deliberately, before it
  is trusted. An assertion that cannot go red is decoration.

## What bad looks like

- A linter plugin nobody can read, that flags things nobody understands,
  and gets `# noqa`'d into silence.
- A rule with a growing allowlist of exceptions. Three exceptions means the
  rule is wrong, not that the code is.
- Guards that duplicate what the type checker already proves.

## How ALFRED does it

Seven guards across `tests/test_architecture.py` and
`tests/test_repo_hygiene.py`:

| Guard | Property |
|---|---|
| domain import allowlist | the domain does no I/O |
| port independence | a port is a bare protocol |
| clock-only time | behaviour is testable at any date |
| composition root only | adapters are wired in one place |
| `structured_call` chokepoint | no unvalidated model output |
| dispatcher chokepoint | no ungated tool call |
| no bare collection strings | a typo cannot open a parallel universe |
| no git-ignored source | a working copy equals a clone |
| every agent folder ships | the loader and git agree |

The last one exists because of a real incident. See
[[Finding 001 The gitignore that hid an agent]].

## Verification

The guards are the verification. Their own verification is the mutation
test: introduce the violation, watch the named test fail, revert. Done for
all seven before they landed.

## Sources

- Ford, Parsons, Kua, *Building Evolutionary Architectures*.
- The `import-linter` and `ArchUnit` projects, for prior art on the shape.
