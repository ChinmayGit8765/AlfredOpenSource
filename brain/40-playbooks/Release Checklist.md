---
tags: [playbook]
---

# Release Checklist

Manual today. Automating it is G6 in [[Gap Register]]. Standard:
[[Release and Versioning]].

## Decide the number

Semver, against the contracts rather than the Python signatures. A
**breaking** change is any of:

- a port signature change
- a change to the capability tier truth table, or to what auto-approves
- an agent manifest schema change that invalidates existing folders
- a stored document shape change that older data cannot satisfy
- removing or renaming an owner-facing command

The first two deserve extra thought: they change what the system is allowed
to do without asking, which is the most consequential possible break and
the least likely to look like one in a diff.

## Before tagging

- [ ] `main` is green, including the security workflow.
- [ ] `.venv/bin/python -m pytest -q` passes locally.
- [ ] `ruff check .` and `mypy` are clean.
- [ ] Version bumped in `pyproject.toml` **and** `alfred/__init__.py`.
      Nothing checks that these agree yet, so check by hand:
      ```
      grep -n version pyproject.toml | head -1
      grep -n __version__ alfred/__init__.py
      ```
- [ ] `CHANGELOG.md`: Unreleased section moved under the new version with
      a date, and a fresh empty Unreleased added.
- [ ] Any stored-shape change has migration notes **in the changelog
      entry**, where an upgrader will see them.
- [ ] The README's test-count badge or claim matches reality. It has drifted
      before.

## Build and check the artefact

```
uv build
uv venv /tmp/release-check
uv pip install --python /tmp/release-check dist/*.whl
/tmp/release-check/bin/alfred --version
/tmp/release-check/bin/alfred --help
```

The version printed must equal the tag. This is the check that catches G5.

## Tag

```
git tag -a v0.2.0 -m "v0.2.0"
git push origin v0.2.0
```

## After

- [ ] GitHub release created, changelog section as the body, `dist/`
      artefacts attached.
- [ ] Try the documented install path on a clean machine or container.
      A quickstart that does not work is worse than none.

## Not doing yet, on purpose

- Publishing to PyPI. Deferred until 1.0; the audience installs from git
  and reads the source first, which is the point.
- SBOM and provenance attestation. G8, and it depends on a release
  workflow existing.
