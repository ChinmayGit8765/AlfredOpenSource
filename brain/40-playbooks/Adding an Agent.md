---
tags: [playbook]
---

# Adding an Agent

An agent is a folder. No Python. Reference: `docs/AGENTS.md` and
[[Prompt and Agent Design]].

## The shape

```
agents/<name>/
  manifest.yaml   # contract: identity, triggers, schedule, tool allowlist
  agent.md        # behaviour prompt
  tools/          # optional, agent-specific
  state/          # optional, agent-local, git-ignored
```

`<name>` matches `^[a-z][a-z0-9_-]{1,40}$` and must equal the manifest's
`name`. A test enforces the match.

## The manifest

Copy an existing one. `agents/study` for a domain agent, `agents/qa` for a
meta agent.

The two fields that matter most:

**`allowed_tools`** is the security boundary, not a convenience list. Start
from what the agent genuinely needs and add nothing speculative. The
read-only built-ins are `current_time`, `list_plans`,
`list_recent_outcomes`, `list_agents_state`, `recall_memories`; the
reversible writes are `log_note` and `remember_fact`.

**`capacity_cost`** is priced against the owner's weekly budget, default
20, and the sum across active agents gates what the builder can add. Check
the headroom before you pick a number:

```
python - <<'PY'
import pathlib, yaml
total = sum(
    yaml.safe_load((p / "manifest.yaml").read_text()).get("capacity_cost", 0)
    for p in pathlib.Path("agents").iterdir() if p.is_dir()
)
print(f"{total} of 20 claimed, {20 - total} free")
PY
```

`tests/test_repo_hygiene.py` fails if the fleet leaves no room for the
builder to add even the smallest habit. That test exists because of a real
bug, see [[Finding 001 The gitignore that hid an agent]].

**For a meta agent** (one that works on ALFRED's output rather than the
owner's life), the contract is `capacity_cost: 0`, `emits_plans: false`,
and a read-mostly allowlist. `emits_plans: false` is what actually keeps a
reviewer out of the week it reviews; the prompt only asks.

## The prompt

Follow the structure the shipped agents share:

1. **Identity and scope.** What it owns, and what it does not, named
   explicitly. One sentence to redirect drift.
2. **Before you plan.** Which tools to call first and why.
   `list_recent_outcomes` before every plan: last week's reality sets this
   week's size.
3. **Field discipline.** How to use `day`, `time`, `duration_min`, `load`,
   `details`, `anchor`. The anchor is not optional: an item without an
   existing cue to stack onto is a wish.
4. **Binding domain rules.** Few, and stated as rules.
5. **Capacity discipline.** Smaller when the owner is wrecked, and say why
   in one sentence.
6. **Tone.** No shame, no streaks, no fake urgency. A miss is data about
   the plan.
7. **Closing the loop.** Ask plainly, take the answer at face value.

Write nothing in the prompt that must be true. If it must be true, it goes
in the manifest or the runtime. A prompt asks; only code enforces.

## Verify

```
.venv/bin/python -m pytest -q tests/test_repo_hygiene.py tests/test_agent_loader.py
.venv/bin/python -m alfred.runtime.cli agents
.venv/bin/python -m alfred.runtime.cli chat --fake
```

If the agent set is asserted anywhere (`test_real_repo_agents_load` asserts
the shipped five), update it in the same commit.

## Do not

- Add an unanchored directory name to `.gitignore` that could match
  `agents/<something>`. See [[ADR-0005 Anchor every gitignore pattern]].
- Commit `agents/<name>/state/`. It is owner data and it is ignored.
- Grant a tool "in case it is useful later".
