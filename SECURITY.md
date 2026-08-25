# Security Policy

ALFRED runs on hardware you own and holds one person's whole life: goals,
lapses, health notes, calendars, and whatever the owner has told it. A bug
here is not an inconvenience, it is a disclosure. This document says what is
supported, how the system is meant to fail, and how to report it when it
does not.

## Supported versions

The project is pre-1.0 and single-branch. Only `main` receives fixes; there
are no backports. Pin a commit if you need stability, and read the diff
before you move.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(**Security** tab, then **Report a vulnerability**). That opens a private
advisory visible only to the maintainers.

Please do not open a public issue for anything that lets an attacker read
owner data, execute a tool without confirmation, or reach a system the
owner did not authorise.

Include what you would want if you were fixing it: the version or commit,
the config that reproduces it, whether it needs the owner's own transport
or can be triggered by external content, and what the attacker ends up
able to do.

Expect a first response within a week. This is a personal project, not a
staffed product, and an honest slow acknowledgement beats an SLA nobody
can keep.

## What counts as a vulnerability here

The security model is documented in full in [docs/GOVERNANCE.md](docs/GOVERNANCE.md)
and the binding contracts are in [ARCHITECTURE.md](ARCHITECTURE.md). In
short, these are the properties meant to hold. A reproducible break of any
of them is a vulnerability:

- **Deny by default on tools.** An agent can invoke only the tools its
  `manifest.yaml` lists in `allowed_tools`. A path that reaches `ToolPort`
  without passing the dispatcher's allowlist check is a break.
- **The capability gate holds.** `destructive` tools always confirm.
  `read_only` never needs to. No configuration setting makes a destructive
  action automatic, and an unclassified tool is treated as destructive.
- **External content is never authority.** Content with `external`
  provenance (a calendar invite, an email body, a webhook payload) can
  never auto-execute above `read_only`, set a goal, confirm a pending
  action, approve a proposal, or steer the builder. Prompt injection that
  gets a tool executed without the owner confirming is a vulnerability;
  prompt injection that produces a silly reply is a bug.
- **Owner data stays local.** Nothing leaves the machine except calls to
  the model backend and the transports the owner configured. A path that
  ships memories, plans, or messages anywhere else is a break.
- **Secrets stay out of the repo and out of the logs.** Tokens come from
  the environment (`ALFRED_DISCORD_TOKEN` and friends), never from a
  committed file. A log line or an audit record containing a token or the
  body of a memory is a break.

Things that are **not** vulnerabilities: the model saying something wrong
or unhelpful, an agent producing a bad plan, a crash with no data or
privilege consequence, or anything that requires an attacker who already
has the owner's shell.

## What the project does to keep those true

- The rules above are asserted as tests in `tests/test_architecture.py`,
  which reads the parsed source: the dispatcher chokepoint, the domain's
  purity, and the single composition root are enforced on every run rather
  than trusted to review.
- The governance truth table is tested directly, provenance by provenance
  and tier by tier, in `tests/test_governance.py`.
- CI runs `ruff`, `mypy --strict`, the offline suite with a branch coverage
  floor, `pip-audit` against the installed dependency tree, CodeQL with the
  `security-and-quality` queries, and a secret scan over the full history.
- The dependency list is deliberately short. A self-hosted tool people
  trust has to be auditable in an afternoon, and every new dependency is
  new attack surface someone has to read.

## Running it safely

- Keep tokens in the environment or a `.env` that is never committed;
  `.gitignore` already excludes `.env`, `config/alfred.yaml`, `data/`, and
  the sqlite files.
- Leave `policy.dry_run_cross_system` on until you have watched a connected
  workflow preview correctly a few times.
- Treat every MCP server you wire in as code you are installing, because
  that is what it is. Classify its tools honestly in `config/mcp.yaml`; an
  unclassified tool defaults to `destructive` on purpose.
- Back up `data/` the way you would back up a password manager. It is the
  same category of file.
