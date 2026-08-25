---
tags: [standard, security, tooling]
status: partial
applies-to: [pyproject.toml, .github/workflows/security.yml]
---

# Supply Chain Security

## What it is

Treating everything the project pulls in, at build time and at run time, as
attack surface: direct dependencies, transitive ones, GitHub Actions, and
the MCP servers an owner wires up.

## Why it matters here

The install target is one person's machine, holding their whole life, with
no network segmentation and no EDR. A compromised transitive dependency has
the owner's sqlite file, their tokens, and their outbound network access.

There is also a category most projects do not have: **MCP servers are
dependencies that execute.** Wiring one in is installing software with tool
access, and its tool *descriptions* reach the model as text. A malicious
server can attempt injection through a tool description alone.

## What good looks like

- **A dependency budget.** Each addition is argued, not assumed. See
  [[ADR-0007 A dependency budget]].
- Automated vulnerability audit of the resolved tree, on a schedule as well
  as per push, because a package that was clean on Monday can have a CVE by
  Thursday and personal projects go weeks without a commit.
- Automated dependency updates, grouped so patch churn is one review and
  anything behaviour-changing is its own.
- A secret scan over **full history**, not the tip. A token committed and
  reverted is still a token that was published.
- Static analysis with security queries, not just style.
- GitHub Actions referenced by a version and, ideally, pinned by SHA:
  a mutable tag is a remote code execution primitive in your CI.
- Least-privilege workflow permissions: `contents: read` by default, wider
  only on the job that needs it.
- New MCP servers classified honestly, and read-only until watched.

## What bad looks like

- `curl ... | bash` in a workflow.
- `permissions: write-all`, or the implicit default token scope.
- A lock file nobody regenerates, so the audit runs against a resolution
  from a year ago.
- Adding a dependency for one helper function.
- Trusting an MCP server's own tier classification. The server is the thing
  you are gating.

## How ALFRED does it

Five runtime dependencies: pydantic, pyyaml, ollama, discord.py, rich. Each
maps to something the spec explicitly permits importing rather than writing
(schema validation, model serving, the Discord gateway). The MCP client is
an optional extra.

`.github/workflows/security.yml` runs `pip-audit --strict` on the installed
tree, CodeQL with the `security-and-quality` queries, and gitleaks over
full history, on every push and weekly. `dependabot.yml` groups patch
updates and takes minor and major individually. Workflow permissions
default to `contents: read`.

`config/mcp.example.yaml` ships capability-tier classifications, and
unclassified tools default to `destructive`.

## Verification

The `security` workflow. Partial rather than enforced, honestly:

- Actions are pinned by tag, not by SHA. A tag can move.
- There is no SBOM, and no provenance attestation on release artefacts.
- There is no lock file, which is defensible for an installable tool but
  means the audit tests the resolution of the day.

All three are in [[Gap Register]].

## Sources

- SLSA v1.0 build levels.
- OpenSSF Scorecard checks, particularly Pinned-Dependencies and
  Token-Permissions.
- The `xz-utils` backdoor (CVE-2024-3094), as the reference case for why a
  quiet transitive dependency deserves attention.
