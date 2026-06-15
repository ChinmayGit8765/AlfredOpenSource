# Governance

ALFRED can act on the world, so every action passes through one gate. This
document is the operator's view of that gate: what runs automatically, what
asks first, what is recorded, and where the off switches actually are.
The implementing code is `alfred/domain/governance.py` and
`alfred/domain/dispatch.py`; the binding contract is in ARCHITECTURE.md.

## Capability tiers

Every tool carries a `CapabilityTier` on its spec:

- `read_only`: can only look. Listing plans, reading the time.
- `reversible_write`: changes something that can be undone or ignored,
  like appending a note.
- `destructive`: changes something that cannot be cleanly undone, or is
  not classified. Unclassified tools default to destructive deliberately;
  an unknown capability gets the strictest gate, never a free pass.

Whether a call executes immediately or waits for you depends on the tier
and on where the instruction came from. The truth table, binding:

| Tier | Owner-initiated | Scheduler-initiated | External content |
|---|---|---|---|
| `read_only` | auto | auto | auto |
| `reversible_write` | auto*, audited | auto*, audited | confirm |
| `destructive` | confirm | confirm | confirm |

\* automatic only while `policy.auto_approve_reversible` is true (the
default). Set it false in `config/alfred.yaml` and reversible writes
confirm too. There is no setting that makes destructive actions automatic.

One more gate sits on top of the tier table: `policy.dry_run_cross_system`
(on by default). While it holds, any write that reaches an external system
(a tool whose source is an MCP server, not a built-in local tool) is
previewed for your confirmation before it runs, even when its tier would
auto-approve. Read-only cross-system calls are unaffected. This is the "dry
run until trusted" stance: when you trust a connected workflow to act
without a preview, set it false. Until any MCP server is configured, no
tool is cross-system, so the gate is inert.

## Provenance and prompt injection

Every inbound instruction carries a provenance: `owner` (you, through a
transport that has verified you), `scheduler` (the heartbeat), or
`external` (content from a connected outside system).

The stance: all external content is untrusted prompt-injection surface. A
calendar invite, an email body, a webhook payload, anything a third party
can write, may contain text that tries to instruct ALFRED. The policy
therefore hard-codes that external provenance never auto-executes anything
above `read_only`, regardless of every other setting. A planted "delete all
my events" can at worst create a pending action that sits in front of you,
named and attributed, until you confirm or deny it.

Today's transports only produce `owner` and `scheduler` provenance (the
Discord adapter ignores every author except the configured owner). The
external lane exists now so that when connectors arrive, the gate is
already in place rather than retrofitted.

## Pending actions: confirm and deny

When a tool call is gated, it is not executed. It becomes a pending action
persisted with an id, and the agent's reply tells you it is waiting:

```
These actions need your confirmation before they run:
- 3f2a91c40d77: calendar.delete_event (clearing the cancelled series)
Say 'confirm <id>' to execute one, or 'deny <id>' to reject it.
```

In chat (terminal or Discord):

- `confirm <id>` executes the action. Before running, the dispatcher
  re-checks the allowlist against the agent's CURRENT manifest: if the
  agent no longer exists, or the tool was removed from its allowlist since
  gating, the confirmation is refused and audited. The world at
  confirmation time wins, never the snapshot from gating time.
- `deny <id>` rejects it; the action never runs.
- `status` shows the count of pending actions.

Unresolved actions expire after `policy.pending_action_ttl_hours` (default
24). A stale action cannot be confirmed: the world may have moved on since
it was gated, so a late `confirm` refuses and the action is marked expired.

## Proposals: self-change with a human in the loop

ALFRED never modifies itself silently. Changes to its own prompts,
manifests, lifecycles, or roster of agents travel as proposals, created
mostly by the periodic reflection (including every deterministic lifecycle
transition) and stored as pending until you rule. Proposal kinds:
`prompt_change`, `manifest_change`, `lifecycle_change`, `new_agent`,
`retire_agent`.

Commands:

- `proposals` lists what is pending, with ids and summaries.
- `approve <id>` approves and, where the runtime knows how, applies:
  `new_agent` writes the agent folder, `lifecycle_change` updates the
  manifest, `prompt_change` rewrites `agent.md`. Kinds the runtime cannot
  apply (`manifest_change`, `retire_agent`) are marked approved and left
  for you to apply by hand; ALFRED says so explicitly.
- `reject <id>` rejects; nothing changes.

Proposals that touch safety settings (allowlists, permissions) carry
`touches_safety` and demand double confirmation. A plain approve is
refused with an explanation; you must say:

```
approve <id> confirm-safety
```

Two more invariants enforced in code: no proposal is ever created with any
status other than pending, whatever its author claimed; and approval only
marks status, so a crash between approval and application can never leave a
half-applied change unrecorded.

## The audit trail

Every governance decision lands in the `audit` collection of the local
database as an append-only record: `{"event": ..., "at": <iso timestamp>,
...}`. Recorded events:

| Event | When | Extra fields |
|---|---|---|
| `tool_denied` | allowlist refusal, vanished agent, or revoked grant at confirm time | agent, tool, provenance, detail, action_id |
| `tool_not_found` | a call named a tool that does not exist | agent, tool, provenance |
| `tool_gated` | a call was held for confirmation | agent, tool, tier, provenance, action_id |
| `tool_executed` | a call actually ran (auto or confirmed) | agent, tool, tier, provenance, ok, action_id |
| `agent_run` | one agent run completed | agent, provenance, rounds, tool_calls, plan_id |
| `pending_action_resolved` | a gated call was confirmed or denied | action_id, agent, tool, tier, approved |
| `pending_action_expired` | a gated call lapsed past its TTL unruled | action_id, agent, tool, tier |
| `proposal_created` | a self-change proposal was filed pending | proposal_id, kind, agent, touches_safety |
| `proposal_resolved` | a proposal was approved or rejected | proposal_id, kind, agent, touches_safety, approved |
| `proposal_applied` | an approved proposal was applied to memory/disk | proposal_id, kind, agent, touches_safety |

Who, what, tier, provenance, verdict: enough to reconstruct any decision
after the fact. Audit records are never deleted by ALFRED.

## Allowlists: least privilege, owner-widened only

Each agent's manifest declares `allowed_tools`, and the dispatcher checks
it before anything else, before even resolving whether the tool exists, so
an agent cannot probe the tool inventory outside its grant. Deny by
default: an empty list is no tools at all, and every new builder-created
agent starts empty.

Exactly two things can widen an allowlist, and both are you: editing the
manifest by hand, or approving a `touches_safety` proposal with
`confirm-safety`. Nothing else, not an agent, not the reflection, not an
MCP server, can grant access. Connecting an MCP server grants nothing by
itself: its tools appear in the inventory (namespaced `<server>.<tool>`,
unclassified ones destructive), but no agent can call them until you name
them in that agent's allowlist. Both gates must open.

## Kill switch reality

Honest inventory of the off switches, strongest first:

- **The process is yours.** ALFRED runs as a foreground process on your
  machine. Ctrl-C ends it; nothing keeps running anywhere else. This is
  the real kill switch.
- **`alfred stop` in chat** asks for a clean shutdown. In the terminal
  REPL (`alfred chat`) it ends the session immediately; in `alfred run` a
  watcher task watches the stop flag and brings the whole service down, so
  the kill switch works from any transport, not only Ctrl-C.
- **Lifecycle pause** stops one agent without touching the rest: set
  `lifecycle: paused` in its manifest and restart. Paused agents never
  route, never schedule, never check in.
- **`deny <id>`** stops any single gated action, and the 24-hour TTL stops
  forgotten ones by default.
- **`heartbeat.quiet_hours`** silences proactive behaviour on a daily
  schedule without stopping anything.

Set `heartbeat.enabled: false` to disable all proactive behaviour while
still running `alfred run`; ALFRED then only reacts, never initiates.

## Local sovereignty defaults

- **The brain stays home.** The model backend is Ollama at
  `http://127.0.0.1:11434` by default. No prompt, plan, or profile leaves
  your machine for inference.
- **Secrets live in the environment only.** The Discord token is read from
  `ALFRED_DISCORD_TOKEN` (configurable via `discord.token_env`), never
  from a config file, and is never logged. `.env`, `config/alfred.yaml`,
  and the database files are gitignored so credentials and personal state
  cannot be committed by accident.
- **State is one local file.** Everything ALFRED knows lives in
  `data/alfred.db` (SQLite). Copy it to back up; delete it to forget.
- **One owner.** The Discord adapter processes messages from
  `discord.owner_id` only. Everyone else is ignored silently, not refused,
  so the bot leaks nothing about itself to strangers.
