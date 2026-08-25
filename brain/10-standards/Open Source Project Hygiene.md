---
tags: [standard, people]
status: enforced
applies-to: [.github, LICENSE, CONTRIBUTING.md, SECURITY.md]
---

# Open Source Project Hygiene

## What it is

The files a stranger looks for in the first two minutes, and what their
absence tells them.

## Why it matters here

Trust is the product. Someone is deciding whether to give this software
their health notes and a Discord token. They do that by skimming: is there
a licence, is there a security policy, does CI pass, is the dependency list
short enough to read, does anyone respond to issues.

Every missing file is an answer to a question they did not get to ask.

## What good looks like

- **LICENSE**, an OSI licence, with the same identifier in
  `pyproject.toml`.
- **README** that leads with what it is and what it is not, and shows the
  thing running.
- **CONTRIBUTING** with the actual ground rules, not boilerplate. The
  useful version says what will get a PR rejected.
- **SECURITY.md** with a private reporting channel and, more usefully, a
  statement of what counts as a vulnerability. Without that, reports arrive
  with the wrong severity and the real ones get lost.
- **CODEOWNERS**, listing the load-bearing files explicitly, so a PR that
  quietly touches the governance gate is visible in the reviewer list.
- **Issue templates** that ask for what a maintainer actually needs, and
  which route security reports away from the public tracker.
- **A PR template** that carries the project's binding rules as
  checkboxes.
- A **CHANGELOG**.
- Green CI on `main`, which is a claim about the maintainer as much as the
  code.

## What bad looks like

- A red badge on the README. It says the maintainer stopped looking, and
  everything else in the repo is read in that light.
- A CoC and templates copied from a template repo, unedited, which read as
  process theatre.
- SECURITY.md with an email address and nothing else, so every crash report
  arrives as a "vulnerability".
- A CONTRIBUTING that explains how to use git.

## How ALFRED does it

MIT licence. README leading with the thesis and a terminal recording.
CONTRIBUTING stating the four ground rules, including "new dependencies
need a reason" and "if your change needs Ollama to test, redesign it
against the fakes". SECURITY.md enumerating the five properties that count
as vulnerabilities and the things that explicitly do not. CODEOWNERS
listing the dispatcher, governance, the composition root, and their tests
individually. Issue forms that lead with a redaction warning, because a log
from this system can contain someone's memories.

Notably absent by choice: a Code of Conduct. Worth adding when there is a
community to govern; today it would be a file about managing contributors
who do not exist. In [[Gap Register]] as a low-severity item rather than
pretended away.

## Verification

The `test_no_source_file_is_git_ignored` guard covers a specific failure in
this category: a governance file that exists on the maintainer's disk and
in no clone. Beyond that, this standard is checked by reading, and the
audit records the reading.

## Sources

- OpenSSF Scorecard, which is essentially this list made machine-checkable.
- The CHAOSS project's metrics on newcomer experience.
