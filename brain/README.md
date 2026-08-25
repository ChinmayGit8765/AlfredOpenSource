# The ALFRED brain

An [Obsidian](https://obsidian.md) vault holding the engineering standards
this project is built against, the decisions taken under them, and a live
audit of the repository's actual state.

Open it by pointing Obsidian at this folder (`Open folder as vault`). The
`.obsidian/` config is committed, so the graph colours, the template
folder, and the link style are the same for everyone. Per-machine state
(`workspace.json`, the cache) is git-ignored.

It is plain Markdown. Nothing here needs Obsidian to be readable, and
nothing in `alfred/` imports it.

## Layout

| Folder | What lives there |
|---|---|
| `00-maps/` | Entry points. Start at [[Brain Home]]. |
| `10-standards/` | One note per standard: what it is, why it matters for a system like this one, what good looks like, and how to verify it. |
| `20-decisions/` | Architecture decision records. Numbered, dated, immutable once accepted. |
| `30-audit/` | This repository measured against the standards, plus the threat model and the open gap register. |
| `40-playbooks/` | The recurring procedures, written down so they are the same every time. |
| `90-templates/` | Note templates for the above. |

## The rule that keeps it honest

A standards note that nobody checks against reality becomes a wish list.
Every note in `10-standards/` ends with a **Verification** section naming
the command, the test, or the CI job that proves the standard holds, and
[[Repository Audit]] records what that check currently says. If a standard
cannot be verified, it is an opinion, and it is labelled as one.

`tests/test_brain_vault.py` keeps the vault's internal links from rotting.
