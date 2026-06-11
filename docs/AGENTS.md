# Writing and growing agents

An agent is a folder. Everything ALFRED knows about an agent comes from two
files inside it, discovered at startup by scanning `agents_dir` (default
`agents/`). This document covers writing one by hand, how the Adaptive
Agent Builder writes one for you, what the lifecycle states mean, and how
to pause or retire an agent yourself.

## Folder layout

```
agents/<name>/
  manifest.yaml   # the contract: identity, triggers, schedule, allowlist
  agent.md        # the behaviour prompt
  tools/          # optional, reserved for future agent-local tools
  state/          # optional, agent-local scratch (gitignored)
```

Discovery is forgiving: a folder with bad YAML, an invalid manifest, or a
missing `agent.md` is logged at warning level and skipped, never fatal.
Only immediate subdirectories of `agents_dir` containing `manifest.yaml`
are considered.

## manifest.yaml: every field

The manifest is validated strictly (`extra="forbid"`): an unknown or
misspelled key is an error at load time, so a typo in `allowed_tools` fails
loudly instead of silently granting nothing.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | str | yes | Unique id and folder name. Must match `^[a-z][a-z0-9_-]{1,40}$`: lowercase letter first, then lowercase letters, digits, `_`, `-`, total 2 to 41 chars. |
| `description` | str | yes | What the agent owns, in plain language. Shown in `agents` listings and used by the builder when proposing. |
| `version` | int | no (1) | Manifest version. Bump it when you change the contract. |
| `domain` | str or null | no | Informal grouping label (e.g. `training`, `academics`). Display only. |
| `shape` | enum or null | no | What kind of thing the agent optimises: `habit`, `skill`, `project`, `state`, `metric`. Habit and state shapes count against the builder's WIP limit while forming. |
| `lifecycle` | enum | no (`established`) | Current lifecycle state; see the table below. `paused` and `retired` agents never route and never get scheduled. |
| `triggers.keywords` | list[str] | no ([]) | Words that route a message to this agent. Matching is case-insensitive on word boundaries. Empty keywords are ignored. |
| `triggers.always` | bool | no (false) | When true the agent claims every inbound message. Always-on agents run before keyword matches. |
| `schedule.kind` | enum | no (`none`) | `none`, `daily`, `weekly`, or `interval`. Anything other than `none` makes the heartbeat run the agent proactively (reason `schedule`, a planning run). |
| `schedule.time` | str or null | no | `"HH:MM"` local, for `daily` and `weekly`. The job fires on the first heartbeat tick at or after this time. |
| `schedule.days` | list[str] | no ([]) | For `weekly`: day names, matched by 3-letter prefix (`mon`..`sun`). |
| `schedule.every_minutes` | int or null | no | For `interval`: fire when at least this many minutes have passed since the last run. |
| `allowed_tools` | list[str] | no ([]) | The security allowlist. The agent may invoke only tools named here; everything else is refused and audited. Deny by default: an empty list means no tools at all. MCP tools use their namespaced name (`<server>.<tool>`). |
| `capacity_cost` | int 0..20 | no (0) | Weekly capacity points this agent's plans claim, out of the profile's `weekly_capacity` (default 20). The builder uses the sum across active agents in its capacity check. |
| `model` | object or null | no | Per-agent generation overrides: `model`, `temperature`, `max_tokens`. Null means the configured defaults. |

A minimal working manifest:

```yaml
name: hydration
description: A tiny daily nudge to drink a glass of water after breakfast.
shape: habit
lifecycle: forming
triggers:
  keywords: [water, hydration]
schedule:
  kind: daily
  time: "08:00"
allowed_tools: []
capacity_cost: 1
```

Built-in local tools you can grant: `current_time`, `list_plans`,
`list_recent_outcomes`, `list_agents_state` (all read-only) and `log_note`
(reversible write).

## agent.md: the behaviour prompt

The prompt is the whole personality and rulebook of the agent. At run time
it is wrapped with the governance preamble, the user-model summary, an
adherence hint when follow-through is slipping, the specs of the
allowlisted tools, and the structured output contract; you write only the
agent-specific part. The three shipped agents (`agents/training`,
`agents/study`, `agents/build`) are the reference. What they have in
common, distilled:

- **Identity and a single domain.** Open by stating exactly what the agent
  owns and naming what it does not. The shipped agents redirect drift in
  one sentence and return to their lane.
- **A before-you-plan ritual.** Tell the agent which tools to call first
  and why: `list_recent_outcomes` before every plan (last week's reality
  sets this week's size), `list_plans` for what was promised,
  `current_time` for day placement, `log_note` for anything the next
  planner should know.
- **Plan item field discipline.** Spell out how to use `day`, `time`,
  `duration_min`, `load` (1 to 5, calibrated honestly), `details` (concrete
  enough to act with zero decisions left), and `anchor` (the existing cue
  the item stacks onto; "a session without an anchor is a wish").
- **A capacity ceiling.** Total plan load stays within the manifest's
  `capacity_cost`, even when the owner is enthusiastic. "Enthusiasm is week
  one; the plan is for week six."
- **Binding domain rules.** The non-negotiables of the domain: the training
  agent never programs through injury and deloads on misses; the study
  agent plans backwards from named exam dates and favours active recall;
  the build agent ships one smallest visible slice per week and enforces
  one project at a time.
- **Tone rules.** Direct and warm; zero shame, zero fake urgency, no streak
  talk. A missed week is information about the plan, not about the owner;
  the only response to a miss is a smaller plan.
- **Closing the loop.** Ask for outcomes in plain language, one item at a
  time, and take "done", "skipped it", "half of it" at face value.

## How the builder creates agents

`new agent <goal>` (or `optimise <goal>` / `optimize <goal>`) in chat
starts a builder session. The conversation is a persisted state machine:

1. **Eliciting.** Before anything is built, the builder interrogates the
   stated goal with one short probing question at a time, because the
   stated goal is rarely the real lever ("read more" is often "get off my
   phone at night"). It advances only once the real lever is clear.
2. **Classifying.** The lever is classified as one shape: habit, skill,
   project, state, or metric. The builder states what it heard and asks you
   to confirm or correct.
3. **Designing.** One structured call produces a full blueprint (manifest
   plus prompt). Non-negotiable rules are then enforced in code, whatever
   the model wrote.
4. **Capacity check.** The blueprint's `capacity_cost` plus all active
   agents' costs is compared against your profile's `weekly_capacity`, and
   the WIP limit is re-checked. If it does not fit, the builder says so
   honestly and offers to shrink or drop something; replying with `force`
   overrides with eyes open.
5. **Proposing / awaiting approval.** You get a plain-language proposal:
   what, the real lever, when it checks in, how small it starts, its
   anchor, and what it will NOT do. Say `yes` (or `approve`, `ok`, etc.) to
   ship, `no` (or `reject`, `cancel`, etc.) to drop, or anything else as
   revision feedback; the blueprint is revised and re-proposed.
6. **Done.** On approval the lifecycle flips to `forming`, the runtime
   writes the folder to disk (it refuses to overwrite an existing one), and
   the agent goes live immediately.

What the builder enforces in code, regardless of what the model designs:

- **WIP limit**: at most 2 agents of shape habit or state in `forming` or
  `reshaped` at once. At the limit the build is refused before any
  questions are asked, with an honest explanation and the offer to shrink,
  pause, or retire something first.
- **Least privilege**: `allowed_tools` is always reset to empty. You grant
  tools deliberately, later, by editing the manifest.
- **Smallest viable size**: habits get their `capacity_cost` clamped to
  1..4 and a daily check-in schedule; the design prompt demands the version
  almost too small to fail.
- **Anchors**: the prompt must cover identity, scope, smallest viable size,
  anchor cue, tone, and output rules; missing sections are appended from a
  standard block.
- **Safe naming**: names are slugified to the manifest pattern and
  de-duplicated against existing agents.
- **Lifecycle**: blueprints are `proposed` until your approval makes them
  `forming`. Nothing goes live without a yes.

While a builder session is open, every non-command message you send
continues it; closed sessions tell you to say `new agent <goal>` again.

## Lifecycle semantics

Support scales inversely with automaticity: the newer or shakier the
behaviour, the more often ALFRED checks in. Check-ins fire from the
heartbeat (so only while `alfred run` is up).

| State | Meaning | Check-in cadence | How you enter / leave it |
|---|---|---|---|
| `proposed` | Designed, not yet approved | none | Builder output. Your approval in the builder conversation moves it to `forming`. |
| `forming` | New behaviour bedding in, maximum support | daily | From `proposed` on approval, or from `lapsing` on recovery. To `established` once 14+ outcomes are logged at a completion rate of 0.8+; to `lapsing` at 2 consecutive misses. |
| `established` | Reliable, lighter touch | every 3 days | To `maintenance` once 30+ outcomes at rate 0.85+; to `lapsing` at 2 consecutive misses. |
| `maintenance` | Near-automatic, minimal support | every 7 days | To `lapsing` at 2 consecutive misses. |
| `lapsing` | Repeated misses; diagnosis mode, never blame | daily | Entered from any active state at 2 consecutive misses. Returns to `forming` (a gentle rebuild, not a jump back) once the miss streak is broken and the overall rate is 0.5+. |
| `reshaped` | Redesigned after a lapse diagnosis | daily | Set by an approved proposal or by hand. Promotes to `established` on the same terms as `forming`. |
| `paused` | Deliberately on hold, no guilt | none | Owner only. Never automatic. |
| `retired` | Honourably closed | none | Owner only. Never automatic. Retiring a goal that no longer earns its place is a success. |

Transitions are deterministic (`domain/lifecycle.next_lifecycle`) and
conservative: when unsure, the state stays put, and `proposed`, `paused`,
and `retired` never auto-transition. Crucially, the system does not apply
transitions silently: the periodic reflection computes them and emits each
one as a `lifecycle_change` proposal that you approve or reject in chat
(`proposals`, then `approve <id>`). The single exception is builder
approval, which moves `proposed` to `forming` directly because you just
said yes to exactly that.

One miss is fine and triggers nothing; the second consecutive miss is the
signal. A lapsing agent gets a diagnosis (mis-sized, mis-cued, life event,
or wrong goal) and the smallest fix: shrink, re-anchor, pause, reshape, or
honestly retire.

## Pausing or retiring by hand

Edit the agent's `manifest.yaml` and set:

```yaml
lifecycle: paused    # or: retired
```

Then restart ALFRED (agent folders are scanned at startup). A paused or
retired agent stays on disk with its history intact, but it no longer
routes messages, never gets scheduled or checked in on, and does not count
against the WIP limit. To resume a paused agent, set `lifecycle: forming`
(a gentle restart) rather than jumping straight back to `established`. To
remove an agent entirely, delete its folder; prefer `retired` so the record
of what was tried survives.
