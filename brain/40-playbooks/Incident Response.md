---
tags: [playbook]
---

# Incident Response

For the owner of a running instance, and for the maintainer receiving a
report. Standard: [[Secrets and Credential Handling]], [[Threat Model]].

## A token leaked

A Discord or Telegram bot token in a commit, a screenshot, a log, or a
pasted issue.

**Assume it is public the moment it left your machine.** Crawlers watch
public commits within seconds, and a revert does nothing: the commit is
still fetchable, and forks have it.

1. **Rotate first, investigate second.** Regenerate the token in the
   Discord or Telegram developer portal. This invalidates the leaked one
   immediately and is the only step that actually stops the bleeding.
2. Update the environment variable, restart.
3. Check what the token could reach while it was live. A Discord bot token
   is a channel into conversations that contain the owner's memories, so
   treat this as a possible content disclosure, not just a credential one.
4. Only then worry about history. Removing it is optional once rotated, and
   `git filter-repo` on a published repository breaks everyone's clone.
5. Add whatever would have caught it: the gitleaks job scans full history,
   `detect-private-key` runs in pre-commit.

## An action ran that should not have

The governance gate exists to make this rare. If it happens:

1. `alfred stop` immediately. It sets the shutdown event and the service
   comes down at once.
2. Read the audit trail. Every dispatched call is recorded, allowed and
   denied, with the agent and the provenance. That record is the
   investigation.
3. Establish the provenance of the instruction. If it was `external`, this
   is a governance failure and a security bug: external content is never
   supposed to reach an executing tool above `read_only`. Report it
   privately per `SECURITY.md`.
4. If it was `owner`, it was a confirmation. Look at the prompt that was
   shown. Confirmation fatigue is a named residual risk in
   [[Threat Model]], and the fix is usually a better preview, not a
   scolding.
5. Turn `policy.dry_run_cross_system` back on for the affected workflow:
   `distrust <agent> <tool>` makes that pair preview again.

## An MCP server is behaving oddly

Unexpected tools, unexpected confirmation prompts, or tool descriptions
that read like instructions to the model.

1. Remove it from `config/mcp.yaml` and restart. Its tools become
   unreachable immediately.
2. Read the audit trail for what it was asked to do and what it returned.
3. Read the server's tool descriptions directly. A description containing
   text addressed to an assistant is an injection attempt, and it is worth
   reporting to whoever maintains the server.

## A vulnerability report arrives

1. Acknowledge within a week. `SECURITY.md` promises that and no more,
   deliberately.
2. Triage against the five properties in `SECURITY.md`. If it breaks one
   of them, it is a vulnerability. If it is a wrong plan or a crash with no
   data consequence, say so kindly and move it to the public tracker.
3. Fix on a private fork through the GitHub advisory, not in the open.
4. Ship the fix, publish the advisory, and add a regression test in the
   same commit as the fix.
5. If the root cause was a rule that existed only in prose, make it a test.
   That is what [[Executable Architecture Rules]] is for, and
   [[Finding 001 The gitignore that hid an agent]] is the worked example.

## The store is corrupt

1. Stop the service before touching anything.
2. Copy `data/` **including** the `-wal` and `-shm` files. Copying only the
   `.db` loses committed transactions still in the write-ahead log.
3. `sqlite3 alfred.db "PRAGMA integrity_check;"`
4. ALFRED tolerates individual bad rows by design: `_decode_doc` and
   `load_or_none` log and skip rather than raising, so a partially damaged
   store still runs. Check the logs for skip lines to see what was lost.
5. Restore from backup if there is one. If there is not, this is the moment
   G1 in [[Gap Register]] stops being theoretical.
