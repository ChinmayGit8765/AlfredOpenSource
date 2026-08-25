---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0005: Anchor every gitignore pattern

## Status

Accepted.

## Context

This one is a post-incident record. See
[[Finding 001 The gitignore that hid an agent]].

`.gitignore` contained `build/`, intended for Python build artefacts. A
gitignore pattern with no leading slash and no internal slash matches **at
any depth**. So it also matched `agents/build/`.

The consequence: the shipped `build` agent, one of five referenced by the
README, `docs/AGENTS.md`, and two tests, was never committed. It existed in
the author's working copy and in no clone. The loader found four agents. Two
tests failed on a fresh checkout, and CI was red on a file that could not
appear in any diff, because git had never heard of it.

This class of bug is invisible to every normal review path. The author sees
a working tree that is correct. The reviewer sees a diff that is correct.
Only a clone reveals it.

## Decision

Every `.gitignore` pattern intended for the repository root carries a
leading slash: `/build/`, `/dist/`, `/data/`, `/config/alfred.yaml`.

Patterns that are genuinely depth-independent (`__pycache__/`, `*.py[cod]`,
`.DS_Store`) stay unanchored, deliberately, because that is what they mean.

The distinction is recorded as a comment at the top of the file, naming the
incident, so the next person to add a pattern knows which kind theirs is.

## Consequences

### What this buys

A directory named `build`, `dist`, or `data` anywhere in the tree is now
tracked normally. Given that `agents/<name>/` is the primary extension point
and owners will name agents whatever they like, this is not hypothetical.

### What this costs

Nothing measurable. Anchored patterns are marginally more to type.

### What we gave up

Nothing.

## Alternatives considered

**Add a negation: `!agents/build/`.** Fixes this instance and leaves the
class. An owner who creates `agents/dist/` hits it again.

**Rename the agent.** Solves the symptom, keeps the trap, and the trap now
has a workaround in the history that looks like a preference.

## Verification

`test_no_source_file_is_git_ignored` in `tests/test_repo_hygiene.py` feeds
every file under the source directories through `git check-ignore --stdin`
and fails on any match, listing the paths. Verified against the original
bug: restoring the unanchored pattern makes it fail and name both files.

`test_every_agent_folder_is_tracked` covers the same class from the other
direction, comparing folders on disk against `git ls-files --cached
--others --exclude-standard`.
