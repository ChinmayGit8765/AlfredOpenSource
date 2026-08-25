---
tags: [standard, operations]
status: enforced
applies-to: [alfred/config.py, config/]
---

# Configuration Management

## What it is

Where settings come from, in what precedence, when they are validated, and
what happens when they are wrong.

## Why it matters here

The twelve-factor advice ("config in the environment") was written for
services deployed by a pipeline. This is software a person installs on
their laptop and edits by hand, so it inverts one thing: **the config file
is the primary surface, and the environment holds only secrets.**

A hand-edited YAML file gets typos. The failure that matters is the silent
one: a mistyped key that pydantic ignores, so the owner sets
`auto_approve_reversible: flase`, sees no error, and believes a gate is on
that is off. Configuration that fails silently in a system with a security
policy is a security bug.

## What good looks like

- **Validated at startup, once, into a typed object.** Not read on demand
  from a dict scattered through the code.
- `extra="forbid"` on every config model, so an unknown key is a loud
  failure rather than a shrug. This is the single highest-value line in a
  config module.
- Clear precedence, documented: defaults, then file, then environment for
  secrets. No layer silently overriding another.
- Secrets **named** in config, valued in the environment: the file says
  `token_env: ALFRED_DISCORD_TOKEN`, so the config is safe to share and
  paste into an issue.
- A shipped `.example` file with every key and a comment, and the real file
  git-ignored.
- A `doctor` command that reports what was loaded and what is missing,
  before anything runs.

## What bad looks like

- Silent defaults for security-relevant settings. If the gate can be off,
  the config must say which state it is in, not leave it to a default the
  owner has never seen.
- A token in the YAML file, which then ends up in a screenshot.
- Configuration read at the point of use, so an invalid value surfaces
  three hours in.

## How ALFRED does it

`alfred/config.py` builds one `AlfredConfig` from `config/alfred.yaml`.
Every one of its eight pydantic models carries `extra="forbid"` through a
shared `_STRICT` config dict, so a mistyped key anywhere in the file is a
startup failure rather than a setting that quietly stays at its default.
`config/alfred.example.yaml` and
`config/mcp.example.yaml` are tracked; the real files are git-ignored.
Transport tokens are referenced by environment-variable name.
`alfred doctor` reports config, model reachability, agent loading, and
transport wiring as a checklist.

The agent manifest uses `extra="forbid"` with a comment stating exactly why:
"a typo in a hand-edited manifest fails loudly at load time instead of
silently dropping a field like `allowed_tools`". Dropping `allowed_tools`
silently would mean an agent with no tools, or worse, a default.

## Verification

`tests/test_config.py`, and `tests/test_agent_loader.py` for the manifest
path. Both cover the unknown-key rejection directly.

Open gap: nothing asserts that a *newly added* config model carries
`_STRICT`. The convention is followed by all eight today and enforced by
nothing. See [[Gap Register]].

## Sources

- The Twelve-Factor App, factor III, read as a starting point rather than a
  rule for installed software.
- pydantic v2 documentation on `model_config` and strictness.
