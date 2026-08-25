---
tags: [audit]
date: 2026-08-25
---

# Gap Register

Every hole named in [[Repository Audit]], ranked by what it costs if it
bites, with a first step small enough to actually get done.

Severity is about the owner, not the developer. **High** means owner data
or owner safety. **Medium** means the system misleads someone. **Low**
means friction.

## High

### G1: No schema migration mechanism

**Standard**: [[Data Durability and Migration]]

Stored documents carry no schema version. `load_or_none` handles drift by
logging and discarding, which is correct for a corrupt row and wrong for an
evolved one: after a field rename the data is present and readable, just
differently shaped, and it gets silently dropped. The owner loses history
and sees only a system that has forgotten things.

This has not bitten yet because no breaking schema change has shipped. It
will bite on the first one, and the loss is unrecoverable.

**First step**: add a `_schema_version` field written by `StorePort.append`
and `put`, defaulted to 1 for existing rows. That alone does not migrate
anything, but it makes migration *possible* later, and it is cheap now
while every row in existence is version 1. Then a `migrations/` module with
per-collection upgrade functions applied on read.

### G2: No evaluation of prompt behaviour

**Standard**: [[Evaluating LLM Behaviour]]

The agent prompts contain the rules that make ALFRED safe to hand a life
to: no shame on a miss, no plan beyond capacity, nothing new in the last 48
hours before an exam. Nothing tests them. A prompt edit that removes one
passes every gate in the repository.

Mitigated more than it looks: capacity, the tool allowlist, and
`emits_plans` are enforced in code, so a model ignoring its prompt still
cannot exceed its allowlist. The unmitigated part is plan *quality* and
tone, which is the product.

**First step**: `tests/evals/`, excluded from `testpaths`, with three
committed scenario fixtures and structural-only assertions against a local
model: schema validity, total load within `capacity_cost`, every item has
an anchor, no tool requested outside the allowlist. Structural assertions
need no judge, so this is about a day and catches the regressions that
matter most.

## Medium

### G3: Nothing asserts secrets stay out of logs

**Standard**: [[Secrets and Credential Handling]], [[Observability for Local Systems]]

The convention is followed carefully: `_decode_doc` logs by key with a
comment saying rows hold personal data, `structured_call` does not log raw
output. Nothing enforces it. A future `logger.debug("config: %s", config)`
would put a Discord token in a file the owner later pastes into an issue.

**First step**: a test that configures logging to a buffer, runs a handful
of representative paths with a sentinel token in the environment and a
sentinel string in a stored memory, and asserts neither appears in the
captured output.

### G4: GitHub Actions pinned by tag, not SHA

**Standard**: [[Supply Chain Security]]

`actions/checkout@v4` is a mutable reference. Whoever controls that tag
controls a step that runs with repository access. This is the OpenSSF
Scorecard Pinned-Dependencies check, and it is the standard supply chain
attack against CI.

**First step**: pin every `uses:` to a full commit SHA with the version in
a trailing comment, and let Dependabot (already configured for
`github-actions`) keep them current.

### G5: Version number lives in two places

**Standard**: [[Release and Versioning]]

`pyproject.toml` declares `0.1.0`; `alfred.__version__` is read at runtime
for the banner and `--version`. Nothing asserts they agree, so a release
can ship reporting the previous version, and the bug report that follows
names the wrong commit.

**First step**: a one-line test comparing `alfred.__version__` against
`importlib.metadata.version("alfred")`.

### G6: No release workflow

**Standard**: [[Release and Versioning]]

Building and publishing is manual, so it is done slightly differently each
time and the steps live in someone's memory.

**First step**: [[Release Checklist]] is written; the workflow that
automates it is not. A tag-triggered job that builds, checks the version
match, and attaches the artefacts to a GitHub release, with PyPI publishing
deliberately left manual until 1.0.

### G7: No test that the store lock holds under concurrency

**Standard**: [[Concurrency and Async Discipline]]

The lock exists and is commented. Its absence would not fail any test, and
losing it means two overlapping writes on one sqlite connection, from
threads nobody is watching.

**First step**: a test firing many concurrent appends through the adapter
and asserting every document lands and no `StoreError` is raised.

## Low

### G8: No SBOM or build provenance

**Standard**: [[Supply Chain Security]]

Nothing lets a user verify what a release was built from. Low today because
releases are manual and there is no published artefact; it rises the moment
one is published.

**First step**: generate a CycloneDX SBOM in the build job and attach it,
once G6 exists.

### G9: No lock file

**Standard**: [[Supply Chain Security]]

Defensible for an installable tool, which should not fight its
environment. The cost is that `pip-audit` tests whatever resolved today, so
a clean audit is a statement about one moment.

**First step**: commit a `uv.lock` used *only by CI*, so the audit and the
tests run against a known resolution while installs stay unpinned.

### G10: Config models rely on convention for `extra="forbid"`

**Standard**: [[Configuration Management]]

All eight carry `_STRICT`. A ninth might not, and a mistyped key in a
security-relevant section would then be silently ignored.

**First step**: a test that walks the config module's `BaseModel`
subclasses and asserts each has `extra="forbid"`.

### G11: No Code of Conduct

**Standard**: [[Open Source Project Hygiene]]

Deliberate. Worth adding when there is a community; today it would govern
contributors who do not exist. Recorded so it is a decision rather than an
oversight.

### G12: Banner variants not asserted equal in length

**Standard**: [[Accessibility and Terminal UX]]

`zip(..., strict=True)` now makes a mismatch crash rather than silently
truncate, but only on the variant being rendered. A short `_BANNER_PLAIN`
would break only on the consoles that use it, which are the ones least
likely to be tested.

**First step**: one assertion that both banners and the fade have equal
length.

## Not gaps

Recorded so they are not re-raised:

- **No formatter.** [[ADR-0002 Lint but do not auto-format]].
- **No telemetry.** [[ADR-0006 No telemetry, ever]].
- **No CRDT or sync.** Out of scope by [[Local First Software]].
- **CLI coverage at 24%.** Intentional, see
  [[ADR-0003 Two coverage floors]].
