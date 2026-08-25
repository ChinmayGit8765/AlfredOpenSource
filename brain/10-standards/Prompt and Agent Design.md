---
tags: [standard, llm, people]
status: enforced
applies-to: [agents, docs/AGENTS.md]
---

# Prompt and Agent Design

## What it is

Treating prompts as source code: versioned, reviewed, structured to a
convention, and constrained by things the code enforces rather than things
the prompt asks for.

## Why it matters here

An agent here is a folder: a `manifest.yaml` and an `agent.md`. No Python.
That is the primary extension point, and it means **most contributions to
this project will be prose**.

Prose in a repository tends to escape review discipline. But an agent
prompt decides what gets scheduled into a real person's week, and the
manifest beside it decides what tools that agent may call. A sloppy prompt
produces a bad week. A sloppy manifest produces an agent with more access
than intended.

The load-bearing insight: **a prompt can only ask, code enforces.** Any
property that actually matters must live in the manifest or the runtime,
not in the prompt's wording. "Never exceed the capacity budget" in an
agent.md is a request the model will sometimes decline.

## What good looks like

For the manifest:

- `allowed_tools` is the security boundary, written deliberately, smallest
  set that works.
- `capacity_cost` priced honestly against the owner's weekly budget, with
  the shipped fleet leaving headroom so the builder can still add
  something on a fresh install.
- `emits_plans: false` on any meta agent, so the executor discards plans it
  emits regardless of how the scheduled prompt is phrased. The prompt asks
  nicely; this flag is what keeps a reviewer out of the week it reviews.
- Unknown keys rejected (`extra="forbid"`), so a typo does not silently
  drop `allowed_tools`.

For the prompt:

- **One domain, stated, with what it does not own named explicitly.**
- A before-you-plan ritual naming which tools to call and why.
- Field discipline spelled out, especially the anchor: an item without an
  existing cue to stack onto is a wish.
- Binding domain rules stated as rules, few enough to be remembered.
- Tone rules, which here are a product requirement rather than polish: no
  shame, no streaks, no fake urgency, a miss is data about the plan.
- Closing the loop: ask for outcomes plainly, take the answer at face
  value.

## What bad looks like

- A prompt that tries to enforce a security property. "Do not use tools
  outside your list" is decoration next to an allowlist.
- Prompts edited without review because "it is just text".
- An agent that owns three domains and therefore owns none.
- Capacity budgets that sum to the owner's entire week across the shipped
  fleet, which makes the builder refuse everything on a fresh install. This
  is a real bug shape, and it is now a test.

## How ALFRED does it

Five shipped agents: `training`, `study`, `build` for the owner's life,
`qa` and `scout` as meta agents. `docs/AGENTS.md` distils the shared
structure and states the meta-agent contract (`capacity_cost: 0`,
`emits_plans: false`, read-mostly allowlists). The Adaptive Agent Builder
emits the same folder shape as a hand-written agent, so there is one format,
not two.

## Verification

`tests/test_repo_hygiene.py`:

- every agent folder has a manifest and a non-empty prompt
- the manifest name matches the folder name
- `allowed_tools` is non-empty
- the shipped fleet leaves capacity for the builder to add an agent

`tests/test_agent_loader.py` asserts the full shipped set loads.

Open gap: no eval harness for prompt behaviour. See
[[Evaluating LLM Behaviour]].

## Sources

- Anthropic's guidance on system prompt structure and on tool definitions
  as part of the security surface.
- The project's own `docs/AGENTS.md`, which is the reference version of
  this note.
