# ALFRED: Build Specification

This is the binding product and engineering specification for ALFRED. Code
review audits the implementation against this document and ARCHITECTURE.md.

## Vision and thesis

ALFRED is a bet on a specific future: the most important AI in a person's
life will not live in a corporate data centre optimising for engagement and
retention. It will run on hardware they own, hold their data, act on their
behalf across every system they authorise, and answer to one loyalty only:
their flourishing.

Big tech's assistants are built to keep the user inside an app. ALFRED is
built to make itself unnecessary, to get someone's life working so well that
the tool fades into the background. It is local-first not as a privacy
gimmick but as a statement of ownership: your intelligence, your keys, your
machine, your side.

That is the destination. Build every decision toward it. But build it in
order, smallest working piece first. A world-changing system that never ships
changes nothing.

## What ALFRED is

ALFRED is a self-hosted personal optimization system that plans and
coordinates the owner's life across domains (training, academics, projects,
and anything else the owner chooses to optimise) using a local LLM. It is
not a chatbot and not a generic assistant. Its job is to take the owner's
goals and current state, decompose them into concurrent plans that do not
conflict, act across the systems in the owner's life, deliver and adjust
those plans through a messaging channel, and hold the owner accountable over
time.

It is its own system: the runtime and orchestration are written from
scratch, not built on an existing agent platform. This is deliberate.

## Prime guardrail: from-scratch system, not from-scratch primitives

- Write yourself: orchestration core, agent model, the Conductor, the
  Adaptive Agent Builder, domain logic, the user model, state schema, the
  model-interface abstraction.
- Import libraries for: model serving (Ollama), schema validation
  (pydantic v2), the Discord gateway (discord.py), persistence (sqlite3),
  and the MCP client.

Never write a bespoke inference engine, websocket protocol, schema
validator, or MCP client.

## Architecture

Clean, layered, ports-and-adapters (see ARCHITECTURE.md):

- Domain layer (pure logic, no I/O): agents, the Conductor, the Adaptive
  Agent Builder, planning, validation, the user model.
- Ports: `ModelPort`, `TransportPort`, `StorePort`, `ToolPort`, `ClockPort`
  (even time is injected).
- Adapters: `OllamaAdapter`, Discord/Telegram/HTTP transports,
  `SqliteAdapter`, local tools, MCP-based tool adapters.
- Dependency injection at a single composition root; the domain never
  imports an adapter.

## The agent model

An agent is a folder under `agents/`, discovered at startup:

```
agents/<name>/
  manifest.yaml   # name, description, triggers, schedule, allowed_tools,
                  # lifecycle state, model overrides
  agent.md        # the role/behaviour prompt
  tools/          # optional, agent-specific tools
  state/          # optional, agent-local state
```

`allowed_tools` is a security allowlist: an agent may only invoke tools it
explicitly declares. Every front-end for creating agents emits this same
folder+manifest.

## The Adaptive Agent Builder

The primary way the owner creates capability. Core stance: a lapse is data
about whether the habit was the right one, the right size, or the right
time. It is never a moral failure. The builder finds the smallest true lever
and then makes itself unnecessary.

Requirements:

- Elicit before building: interrogate the stated goal ("read more" is often
  "get off my phone at night").
- Classify the shape: habit, skill, project, state/avoidance, or metric;
  each scaffolds differently.
- Ground in behaviour: habit stacking, smallest viable size, friction
  tuning, identity framing. Apply, do not lecture.
- Respect capacity: one or two habits forming at once, enforced as a WIP
  limit; refuse to stack while existing habits wobble.
- Lifecycle per agent: Proposed, Forming, Established, Maintenance,
  Lapsing, Reshaped, Paused, Retired. Support scales inversely with
  automaticity.
- Diagnose lapses, do not nag: one miss is fine; catch the second. On
  repeated lapse run a short diagnostic, then shrink, re-anchor, pause,
  reshape, or honestly retire.
- Refuse dark patterns: no streak shame, no fake urgency, no engagement
  maximising.

The builder is also a gardener: it prunes and retires as well as creates.

## The action layer: MCP

No bespoke integrations. ALFRED speaks MCP; every server the owner connects
becomes a capability behind `ToolPort`. The power is composition across
systems in one intent. This is a horizon, not v1: design for the ceiling,
build the floor first.

## Structured output and reliability

Every LLM call that must return structured data uses a pydantic model as
the schema. Validate; on failure retry with the validation errors fed back,
bounded attempts; then fail cleanly. Never trust raw LLM text as structured
data.

## Adaptation and self-improvement

- User model: persisted, structured, versioned; updated from every
  interaction; observations append rather than overwrite.
- Feedback loop: plan, observe outcome, update model, plan better. A plan
  the owner repeatedly ignores is a signal the plan is wrong.
- Periodic reflection: scheduled Conductor review, written to state.
- Self-improvement with a human in the loop: proposals only, surfaced for
  approval, versioned and reversible. Safety fields (allowlists,
  destructive permissions) are never auto-modified.
- Observable and bounded: every adaptation logged with its reason.

## Governance and security

- Capability tiers: read-only, reversible write, destructive. Confirmation
  scales by tier; destructive always confirms.
- Least privilege per agent via manifest allowlists; nothing silently
  widens an allowlist.
- All inbound content from connectors is untrusted (prompt-injection
  surface); externally-triggered actions never auto-execute above
  read-only.
- Dry run before cross-system action until a workflow is trusted.
- Full audit and reversibility; kill switch; undo where the underlying
  system allows.
- Local and sovereign by default: bind 127.0.0.1, credentials in the
  environment, never hardcoded or logged.

## Build order

1. Model round-trip: Python -> Ollama -> pydantic-validated structured
   output, from a terminal.
2. Core + first agent: manifest schema, discovery, orchestration core,
   Training agent, CLI.
3. Transport: discord.py adapter (Telegram and a local HTTP API followed on
   the same TransportPort). v1 ship point: a Training agent folder, messaged
   on Discord, returns a validated weekly plan, which is stored.
4. Conductor + Study and Build agents + concurrent-plan reconciliation.
5. Adaptation, proactivity, accountability: user model, feedback loop,
   heartbeat, reflection, human-in-the-loop proposals.
6. Horizon: MCP action layer (Calendar first), cross-system workflows, full
   conversational Agent Builder.

## Engineering standards

- Python 3.12+, full type hints, pydantic v2 throughout.
- uv for dependency management; minimal justified dependencies
  (`ollama`, `pydantic`, `pyyaml`, `rich`, `discord.py`; `mcp` optional).
- pytest for the domain layer, validation/retry, adaptation, tier gating.
- Structured logging; no print debugging in committed code.
- Small single-responsibility modules; comments explain why, not what.
- Never invent library APIs; verify when unsure.
