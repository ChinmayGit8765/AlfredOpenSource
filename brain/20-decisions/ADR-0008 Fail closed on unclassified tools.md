---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0008: Fail closed on unclassified tools

## Status

Accepted. This decision predates the vault; it is recorded here because it
is the single most consequential line in the governance model and it
deserves an explicit record rather than a sentence in a reference doc.

## Context

Every tool carries a `CapabilityTier`: `read_only`, `reversible_write`, or
`destructive`. The tier decides whether a call executes immediately or
waits for the owner's confirmation.

Tools arrive from two places. Built-in local tools are classified by the
project, in code, correctly. **MCP tools arrive from a server the owner
wired up**, and their classification comes from `config/mcp.yaml`, which
the owner writes by hand.

So there will be unclassified tools. Always. A new server exposes a tool
nobody has categorised, an owner adds a server and does not fill in the
table, or a server adds a tool in an update after the config was written.

The system has to do something with a tool whose tier it does not know, and
this is the whole decision: **treat the unknown as safe or as dangerous.**

The pressure toward "safe" is real and it is about user experience. A
freshly wired calendar server whose every tool prompts for confirmation
feels broken. The temptation is to default unknown tools to `read_only`,
or to infer the tier from the tool's name.

Name inference is worse than it sounds. Tool names and descriptions come
from the MCP server, which is the thing being gated. A server named its
delete operation `list_events_helper` and the gate is bypassed by a string
comparison.

## Decision

An unclassified tool is `destructive`.

It confirms with the owner every time, until someone classifies it
explicitly in `config/mcp.yaml`. No name-based inference, no
description-based inference, no "probably fine".

`docs/GOVERNANCE.md` states it plainly: "Unclassified tools default to
destructive deliberately; an unknown capability gets the strictest gate,
never a free pass."

## Consequences

### What this buys

The gate cannot be bypassed by omission, which is how gates are actually
bypassed in practice. Nobody has to remember to classify a tool for the
system to be safe; forgetting produces friction, not exposure.

It also makes the failure mode legible to the owner. A wall of confirmation
prompts from a newly wired server is a clear signal that says "classify
these", which is a much better teacher than any documentation.

### What this costs

First-run friction on any new MCP server. The owner confirms everything
until they fill in the table.

### What we gave up

A smoother first five minutes with a connector, in exchange for the
property that a connector cannot act unasked.

## Alternatives considered

**Default to `read_only`.** Any unclassified tool acts without asking. One
forgotten config line is one silent destructive action.

**Infer from the tool name or description.** Rejected: attacker influenced,
and the attacker is the thing being classified.

**Ask the model to classify.** Rejected for the same reason plus a second
one: it puts a non-deterministic component inside the security decision,
and the input to that component is text the server controls.

## Verification

`tests/test_governance.py` parametrizes the full tier by provenance by
setting truth table. `tests/test_mcp_tools.py` covers the default applied to
an unclassified MCP tool. `tests/test_dispatch.py` covers the dispatcher
refusing a tool outside an agent's allowlist regardless of tier.
