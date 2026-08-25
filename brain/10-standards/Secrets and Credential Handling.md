---
tags: [standard, security]
status: enforced
applies-to: [.gitignore, alfred/config.py, .github/workflows/security.yml]
---

# Secrets and Credential Handling

## What it is

Where credentials live, how they reach the process, and what guarantees
they never reach the repository, the logs, or a bug report.

## Why it matters here

The tokens involved (a Discord bot token, a Telegram bot token, MCP server
credentials) are not incidental. A Discord bot token is a live channel into
the owner's conversations with a system that knows their health, their
finances, and their failures. Leaking it is not a service outage, it is a
disclosure.

The realistic leak paths for a personal project are boring and all of them
have happened to someone:

1. A token typed into `config/alfred.yaml`, which is committed because it
   was not ignored yet.
2. A token in a debug log, pasted into an issue by an owner trying to get
   help.
3. A token in a screenshot of a terminal.
4. A token committed, noticed, and "removed" in a later commit, which does
   nothing at all.

## What good looks like

- Secrets come from the environment. The config file names the variable,
  it never holds the value, which means the config file is safe to paste
  into an issue.
- `.env` is git-ignored and `.env.example` is tracked with empty values.
- The real config files are git-ignored; `.example` versions are tracked.
- Nothing logs a token, not even truncated. "First four characters" is
  still four characters more than zero.
- A secret scan over **full history** in CI, because removal in a later
  commit is not removal.
- The rotation procedure is written down before it is needed. See
  [[Incident Response]].

## What bad looks like

- `logger.debug("config: %s", config)` where config holds a resolved token.
- A `.env` file added in the same commit that adds it to `.gitignore`, in
  the wrong order.
- Treating a revert as a fix. The commit is still fetchable, forks still
  have it, and crawlers already have it.
- Secrets in CI logs via `set -x` or an echoed environment.

## How ALFRED does it

Tokens are named in config and valued in the environment
(`token_env: ALFRED_DISCORD_TOKEN`). `.gitignore` excludes `.env`,
`/config/alfred.yaml`, `/config/mcp.yaml`, `/data/`, and the sqlite files.
`.env.example` ships with the variable names and no values. The gitleaks
job in `security.yml` checks out with `fetch-depth: 0` so it scans history
rather than the tip. `detect-private-key` runs in pre-commit.

The `.gitignore` patterns for owner data are anchored, for the reason in
[[ADR-0005 Anchor every gitignore pattern]].

## Verification

- gitleaks over full history in CI.
- `detect-private-key` in pre-commit.
- `tests/test_repo_hygiene.py` asserts the inverse property, that no
  *source* file is accidentally ignored.

Open gap: nothing asserts that a token never reaches a log record. That
would be a test that runs the logging setup with a populated environment
and greps the output. See [[Gap Register]].

## Sources

- OWASP Secrets Management Cheat Sheet.
- GitHub's documentation on removing sensitive data, which is mostly a
  document about why you cannot.
