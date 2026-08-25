---
tags: [standard, llm, testing]
status: gap
applies-to: [agents, alfred/domain/executor.py]
---

# Evaluating LLM Behaviour

## What it is

Testing the parts of the system whose output is produced by a model, where
the same input legitimately produces different text every time.

## Why it matters here

[[Testing Strategy]] covers everything deterministic, which is most of the
code. It deliberately does not cover the question "does the study agent
actually plan backwards from an exam date", because that behaviour lives in
a Markdown prompt and a model's response to it.

That behaviour is not decoration. The prompts contain the rules that make
the system safe to hand a life to: no shame on a miss, no plan that
exceeds capacity, no new material in the last 48 hours before an exam. A
prompt edit that quietly removes one of those is invisible to every test
that exists today, and its consequence is a person having a worse week.

Two things need evaluating and they are different:

**Behavioural adherence.** Does the agent follow its stated rules given
realistic inputs? Non-deterministic, needs judgement, cheap to get wrong.

**Structural conformance.** Does the reply validate, does the plan stay
within capacity, does every item carry an anchor, is a tool called that is
not in the allowlist? Deterministic, checkable, and currently unchecked
against real model output.

## What good looks like

- A small corpus of **scenario fixtures**: an owner profile, some recent
  outcomes, a message. Committed, readable, and stable.
- **Structural assertions run against real model output**, in a suite that
  is explicitly opt-in and excluded from the offline run: schema validity,
  capacity ceiling respected, every item has an anchor, no tool outside the
  allowlist requested. These are objective and need no judge.
- **Behavioural checks graded by a model** with a rubric taken from the
  agent's own prompt, reported as a rate rather than a pass or fail,
  because a single sample of a stochastic process is not a result.
- Run on prompt changes and model upgrades, which are the two events that
  move the numbers.
- Never in the required offline suite. It needs a model, it is slow, and
  making it blocking would break the property that the suite runs anywhere
  in seconds.

## What bad looks like

- Asserting exact model output. Fails on every model update, teaches people
  to delete the test.
- A model grading its own output with no rubric, which measures fluency.
- Running evals in the required suite, which either makes CI need a GPU or
  makes the evals get skipped and rot.
- Treating one sample as a measurement.

## How ALFRED does it

It does not, and this note exists to say so plainly. `--fake` mode proves
the *pipeline* end to end with a `DryRunModel`, which is a real and
valuable guarantee: routing, governance, plan validation, and the executor
loop are all exercised. What is unverified is whether a real model, given
a real prompt, produces a good plan.

The gap is narrower than it looks, because the properties that matter most
are enforced in code rather than in the prompt: the capacity ceiling, the
tool allowlist, and `emits_plans` are runtime facts. A model that ignores
its prompt cannot exceed its allowlist. That is the design paying off.

## Verification

None yet. This is the largest open gap in the vault and it is recorded as
such in [[Gap Register]].

Proposed first step, small enough to actually happen: a `tests/evals/`
directory, excluded from `testpaths`, with three scenario fixtures and
structural-only assertions against a local model, run manually on prompt
changes. Structural assertions need no judge, so this is a day of work and
catches the regressions that matter most.

## Sources

- Anthropic's guidance on building evals, particularly preferring
  objective graders and reporting rates over verdicts.
- Hamel Husain's writing on the eval loop for LLM products.
