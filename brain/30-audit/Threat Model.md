---
tags: [audit, security]
date: 2026-08-25
---

# Threat Model

Who attacks a self-hosted personal AI, how, and what actually stops them.
Companion to [[LLM Agent Safety]] and `docs/GOVERNANCE.md`.

## What is being protected

In descending order of consequence:

1. **The owner's ability to act safely.** ALFRED can send messages and,
   through MCP, touch calendars and files. An unauthorised action is worse
   than an unauthorised read, because it is not undoable.
2. **The store.** Memories, plans, outcomes, messages. A person's health,
   finances, academic standing, and failures.
3. **The credentials.** A Discord bot token is a live channel into
   conversations containing (2).
4. **Availability.** Lowest. An ALFRED that is down is an inconvenience.

## Who

| Actor | Capability | Motivation |
|---|---|---|
| **Third-party content author** | Can write text ALFRED will read: a calendar invite, an email body, a webhook payload | Opportunistic; often automated and not targeting this owner at all |
| **Malicious or compromised MCP server** | Supplies tool names, descriptions, schemas, and results | Data exfiltration, action on the owner's systems |
| **Compromised dependency** | Arbitrary code in-process | Credential and data theft at scale |
| **Someone with local access** | Reads the disk | Targeted; a partner, a housemate, a thief |
| **Model backend operator** | Sees every prompt, if remote | Only in scope when the owner points at a hosted model |

Explicitly **not** in scope: a remote network attacker. There is no
listening service by default. The HTTP transport is opt-in and the owner's
responsibility to expose or not.

## The primary attack: indirect prompt injection

The realistic path, step by step:

1. Someone sends the owner a calendar invite. Its description contains
   text addressed to an AI assistant.
2. A connector surfaces that invite. It reaches the model as content.
3. The model, having no reliable way to separate instruction from data,
   treats it as instruction and emits a tool call.

**What stops it, in layers:**

- The invite arrives with `external` provenance, and the policy hard-codes
  that external content never auto-executes above `read_only`. Not
  configurable.
- External content can never set a goal, confirm a pending action, approve
  a proposal, or drive the builder. These are owner-authority paths and
  provenance is checked on each.
- The tool has to be in the requesting agent's `allowed_tools`. Deny by
  default.
- If it is destructive, and unclassified means destructive, it becomes a
  pending action the owner must confirm, showing the tool, the arguments,
  and the agent that asked.
- The whole exchange is audited, including the denial.

**Residual risk.** Injection can still consume the owner's attention: a
flood of plausible pending actions is a denial-of-service on the confirm
prompt, and a tired owner confirming without reading is the realistic
failure. Confirmation fatigue is the weak point of this design, and it is
not solved.

Injection can also still influence *content*: a planted instruction could
shape a plan or a memory without ever calling a tool. A poisoned memory
persists and colours later runs. Nothing currently detects that.

## The second attack: the MCP server itself

An owner wires in a server. It is code, running locally, supplying tools.

- Its tool *descriptions* reach the model as text, so the server can
  attempt injection with no user content involved at all.
- It could name a destructive operation innocuously, which is precisely why
  tier inference from names is refused. See
  [[ADR-0008 Fail closed on unclassified tools]].
- `policy.dry_run_cross_system` previews every cross-system write until the
  owner turns it off per workflow.

**Residual risk.** A server the owner has classified as `read_only` and
trusted, that later updates to do more. Nothing re-checks a server's tools
against their previous classification.

## The third attack: the supply chain

Five runtime dependencies, chosen to stay auditable, and `pip-audit` plus
CodeQL plus gitleaks in CI. See [[Supply Chain Security]].

**Residual risk.** Actions pinned by mutable tag; no SBOM; no lock file.
G4, G8 and G9 in [[Gap Register]].

## The fourth: local access

Out of scope, and worth saying why rather than ignoring. The store is
unencrypted sqlite. Full-disk encryption is the operating system's job and
doing it again in the application would be security theatre with a key that
has to live somewhere on the same disk.

What the project owes here is honesty, and `SECURITY.md` gives it: back up
`data/` the way you back up a password manager, because it is the same
category of file.

## What would change this model

- **Multi-user support.** Would introduce authentication and authorisation,
  which do not currently exist because they do not need to.
- **A default-on network listener.** Would put a remote attacker in scope.
- **A remote model backend as default.** Would move the entire prompt, and
  therefore the entire store over time, into someone else's logs.

All three are currently ruled out by [[Local First Software]] and
`docs/SPEC.md`. They are listed so that anyone proposing one knows they are
reopening this document.

## Sources

- OWASP Top 10 for LLM Applications, 2025.
- Simon Willison on indirect prompt injection and the lethal trifecta.
- NIST AI 100-2 adversarial ML taxonomy.
