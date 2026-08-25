---
tags: [audit]
date: 2026-08-25
commit-range: cc9ba6d..HEAD
---

# Repository Audit

ALFRED measured against every note in [[Standards MOC]]. The point of this
page is that a standards vault with no scorecard becomes a wish list, so
each row records what the check currently says, not what it should say.

**Status meanings.** *Enforced*: a test or a CI job fails when it breaks.
*Partial*: verified in part, with a named hole. *Gap*: the standard is
written down and nothing checks it. *Convention*: true today, held up by
nobody breaking it.

## Numbers at the time of writing

| Measure | Value |
|---|---|
| Source modules | 45 |
| Test modules | 33 |
| Tests, all offline | 575, about 7 seconds |
| Lines of Python | ~10,000 |
| Branch coverage, package | 80.3% (floor 79) |
| Branch coverage, domain and ports | 94% (floor 93) |
| `mypy --strict` errors | 0 across 45 modules |
| `ruff check` findings | 0 |
| `type: ignore` suppressions | 1, coded and commented |
| Runtime dependencies | 5 |
| Architectural guards | 7, each verified against a real violation |

## Scorecard

| Standard | Status | What actually enforces it | Hole |
|---|---|---|---|
| [[Ports and Adapters]] | Enforced | 4 AST guards in `test_architecture.py` | none |
| [[Executable Architecture Rules]] | Enforced | the guards, each mutation-tested | none |
| [[Concurrency and Async Discipline]] | Partial | ruff `ASYNC`, core lock tests | no test proves the store lock holds under concurrent writes |
| [[Error Handling and Failure Modes]] | Enforced | store, schema and structured-call tests | `BLE` blind-except rules not enabled |
| [[Typing Discipline]] | Enforced | `mypy --strict` in CI and pre-commit | none |
| [[Python Packaging and Distribution]] | Enforced | the `build` job installs the wheel clean and runs the entry point | none |
| [[Configuration Management]] | Enforced | `extra="forbid"` on all 8 models, config tests | nothing asserts a *new* model carries `_STRICT` |
| [[Testing Strategy]] | Enforced | 575 offline tests, two coverage floors, warnings as errors | no mutation testing, no property tests for conflict detection |
| [[Structured Output Contracts]] | Enforced | chokepoint guard plus `test_structured.py` | none |
| [[Evaluating LLM Behaviour]] | **Gap** | nothing | no eval harness at all; the largest gap here |
| [[LLM Agent Safety]] | Enforced | dispatcher chokepoint guard, full truth table, provenance tests | see [[Threat Model]] for residual risk |
| [[Supply Chain Security]] | Partial | pip-audit, CodeQL, gitleaks, Dependabot | Actions pinned by tag not SHA; no SBOM; no lock file |
| [[Secrets and Credential Handling]] | Enforced | gitleaks over full history, `detect-private-key`, anchored ignores | nothing asserts a token never reaches a log record |
| [[Privacy and Data Minimisation]] | Enforced | import allowlist, retention sweep tests, `forget` | no test that prompt assembly excludes irrelevant memories |
| [[Data Durability and Migration]] | Partial | WAL, tolerant reads, corrupt-row tests | **no migration mechanism, no schema version on documents**; no backup guidance |
| [[Local First Software]] | Enforced | the offline suite is the running proof | none |
| [[CI CD Pipeline Design]] | Enforced | three jobs plus a security workflow | none |
| [[Observability for Local Systems]] | Partial | audit records on allow and deny, sweep tests, `T20` bans `print` | no assertion that logs exclude owner content |
| [[Release and Versioning]] | Partial | Keep a Changelog with the contract rule stated | version duplicated between `pyproject.toml` and `__version__`; no release workflow |
| [[Documentation Standards]] | Enforced | agents load, examples run, vault links resolve | prose correctness is unverifiable, stated as such |
| [[Open Source Project Hygiene]] | Enforced | all expected files present, CI green | no Code of Conduct, deliberately |
| [[Prompt and Agent Design]] | Enforced | manifest, prompt, allowlist and capacity guards | prompt *behaviour* unverified, see the eval gap |
| [[Accessibility and Terminal UX]] | Enforced | encoding fallback tests, Windows in the matrix, `T20` | banner row counts not asserted across both variants |

## What changed in this pass

Nine of these rows were previously "convention". The work that moved them:

1. **A red `main` was fixed, and the reason it was red was invisible.** See
   [[Finding 001 The gitignore that hid an agent]].
2. **Seven architectural guards** turned the binding rules into tests.
3. **`mypy --strict`** across the package, ten real errors fixed.
4. **ruff** with a curated ruleset, clean.
5. **Two coverage floors**, branch-based.
6. **CI split** into `static`, `test`, and `build`, with a job that
   installs the wheel into a clean environment.
7. **A security workflow**: pip-audit, CodeQL, full-history gitleaks,
   Dependabot.
8. **Governance files**: SECURITY.md stating what counts as a
   vulnerability, CODEOWNERS naming the load-bearing files, issue and PR
   templates carrying the contracts.
9. **Four real bugs** found on the way, listed in
   [[Finding 002 Four latent defects]].

## The honest summary

The domain layer of this codebase is in unusually good shape: pure, fully
typed, 94% branch covered, and now structurally guarded. The things it
promises about safety are true, and most of them are now true by test
rather than by intention.

Three real weaknesses remain, in priority order:

1. **No schema migration story.** Tolerant reads discard a drifted
   document. That is right for corruption and wrong for a field rename,
   where the data is readable and just differently shaped. The first
   breaking schema change will silently drop owner history.
2. **No LLM evals.** The prompts carry the rules that make this system safe
   to hand a life to, and a prompt edit that removes one is invisible to
   every existing test.
3. **Supply chain gaps**: unpinned Action SHAs, no SBOM, no lock file.

All three are in [[Gap Register]] with a proposed first step sized to
actually happen.
