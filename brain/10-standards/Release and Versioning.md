---
tags: [standard, operations]
status: partial
applies-to: [pyproject.toml, CHANGELOG.md]
---

# Release and Versioning

## What it is

What a version number promises, when it changes, and what a release
consists of beyond a git tag.

## Why it matters here

Semantic versioning is a promise about an API. This project's API is not
its Python functions, because nobody imports `alfred` as a library. The
things people depend on are:

- The **agent manifest schema**. Everyone's `agents/` folders are written
  against it. A renamed field breaks every agent an owner has built.
- The **stored document shapes**. An installed system has a sqlite file
  full of them, and an upgrade that cannot read it is data loss.
- The **owner-facing commands**. Muscle memory is an interface.
- The **capability tier truth table**. A change here changes what the
  system is allowed to do without asking, which is the most consequential
  possible breaking change and the least likely to look like one.

So the versioning rule has to be stated in those terms, or semver will be
applied to the Python signatures nobody uses while the manifest schema
changes in a patch release.

## What good looks like

- **Semver against the contracts above**, stated explicitly so nobody
  guesses.
- A changelog written for the person upgrading, in the
  [Keep a Changelog](https://keepachangelog.com) shape, with an Unreleased
  section maintained as changes land rather than reconstructed from git log
  at release time.
- The version in exactly one place, read everywhere else.
- A release that includes: the tag, the changelog entry, built sdist and
  wheel, and, ideally, provenance attestation.
- Migration notes for anything touching the stored shapes, in the changelog
  entry itself where the upgrader will actually see them.

## What bad looks like

- A version string duplicated in `pyproject.toml` and `__init__.py`, which
  drift, and the drift is discovered by a user reporting the wrong version.
- A changelog generated from commit subjects. Commit subjects are written
  for reviewers, changelogs for upgraders; they are different documents.
- Breaking the manifest schema in a patch release because "no Python API
  changed".

## How ALFRED does it

`CHANGELOG.md` follows Keep a Changelog with an Unreleased section, and its
preamble states the rule above: pre-1.0, the contracts in `ARCHITECTURE.md`
are the API, and a change to a port signature, the capability truth table,
or the manifest schema is a breaking change regardless of the number.

## Verification

Weak, and marked partial for it. Open gaps:

- The version lives in `pyproject.toml` while `alfred.__version__` is read
  at runtime for the banner and `--version`. Nothing asserts they agree.
- No release workflow. Building and publishing is manual, which means it is
  done differently each time.
- No test that the changelog was updated on a behaviour-changing PR.

See [[Gap Register]] and [[Release Checklist]].

## Sources

- Semantic Versioning 2.0.0, and Rich Hickey's *Spec-ulation* for why the
  interesting part is what you promise, not how you number it.
- Keep a Changelog 1.1.0.
