---
tags: [playbook]
---

# Wiring an MCP Connector

An MCP server is a dependency that executes and supplies text the model
reads. Treat it as installing software with tool access, because that is
what it is. Reference: `docs/CONNECTORS.md`, [[Threat Model]].

## Before

**Read the server's source, or accept that you are trusting a stranger.**
This is not paranoia for this project specifically: the server's tool
descriptions reach the model as instructions, so a hostile server can
attempt prompt injection with no user content involved at all.

Prefer servers that are read-only for the first pass. You can always widen.

## Wiring it

1. Add the server to `config/mcp.yaml`, modelled on
   `config/mcp.example.yaml`.

2. **Classify every tool.** This is the step that matters and the step
   people skip. Each tool gets `read_only`, `reversible_write`, or
   `destructive`.

   Classify by **what it does**, never by what it is called. Names come
   from the server, and the server is the thing you are gating. See
   [[ADR-0008 Fail closed on unclassified tools]].

   Anything you do not classify is treated as `destructive` and confirms
   every time. That is the system working, not a bug: the wall of
   confirmation prompts is telling you to finish the table.

3. **Leave `policy.dry_run_cross_system` on.** Every cross-system write
   previews for confirmation, whatever its tier, until you turn it off for
   a workflow you have watched behave correctly several times.

4. Grant the tools to an agent by adding them to that agent's
   `allowed_tools`. A configured tool that no agent lists is unreachable,
   which is the correct default.

## Verify

```
.venv/bin/python -m alfred.runtime.cli doctor
```

`doctor` probes the server, lists the tools it exposes, and shows the tier
each resolved to. Read that list. If a tool you expected to be
`reversible_write` shows as `destructive`, you missed a classification.

Then exercise it once, deliberately, and watch what the preview says before
you confirm.

## Ongoing

- A server update can add tools. They arrive unclassified, therefore
  destructive, therefore confirming. Re-run `doctor` after any update.
- Nothing currently re-checks a server's existing tools against their
  previous classification. A server that changes what a tool *does* while
  keeping its name is a residual risk named in [[Threat Model]].
- A sudden increase in confirmation prompts is a signal, not an annoyance.
  Look before you clear it.

## Do not

- Classify by name pattern.
- Disable `dry_run_cross_system` globally to reduce friction. Turn it off
  per workflow, once, deliberately.
- Wire a server that requires credentials broader than the task. An MCP
  server with full account access is full account access.
