---
tags: [finding]
severity: high
status: fixed
found: 2026-08-25
---

# Finding 001: The gitignore that hid an agent

## What is wrong

`.gitignore` contained `build/`, meant for Python build artefacts. A
gitignore pattern with no leading slash and no internal slash matches at
**any depth**, so it also matched `agents/build/`.

The `build` agent, one of the five shipped agents, was therefore never
committed.

## How it fails in practice

- The README says "five hand-written agents (training, study, build, plus
  two meta)". `docs/AGENTS.md` names `agents/build` as one of the three
  reference agents and describes its rules. Neither was true of any clone.
- `tests/test_agent_loader.py::test_real_repo_agents_load` and
  `tests/test_core.py::test_build_system_fake_smoke` both assert the set
  `{build, qa, scout, study, training}`. Both failed on a fresh checkout.
- CI on `main` was therefore red, on a file that could not appear in any
  diff because git had never heard of it.
- Any owner cloning the repository got four agents and no error, only a
  documented agent that silently did not exist.

The reason this survived is what makes it worth a finding: **it is
invisible from every normal vantage point.** The author's working tree is
correct. The reviewer's diff is correct. Only a clone reveals it, and
nobody clones their own repository.

## Evidence

```
$ git check-ignore -v agents/build/manifest.yaml
.gitignore:7:build/     agents/build/manifest.yaml
```

## Standard it violates

[[Executable Architecture Rules]], in the specific sense that a property
everyone assumed ("what I see is what ships") was checked by nothing.

## Fix

1. Anchor the build-artefact patterns: `/build/`, `/dist/`, and the same
   for the owner-data patterns `/data/`, `/config/alfred.yaml`. See
   [[ADR-0005 Anchor every gitignore pattern]].
2. Restore `agents/build/` with a manifest and a prompt matching what
   `docs/AGENTS.md` describes: one project at a time, one smallest visible
   slice per week, cut scope rather than add days.
3. Price it at `capacity_cost: 4` rather than the 6 the two skill agents
   claim. At 6 the shipped fleet would spend 18 of the default 20 weekly
   points, and the builder refuses any blueprint pushing the active total
   over the owner's capacity. `new agent` would have been dead on arrival
   on a fresh install. This is [[Finding 002 Four latent defects]] adjacent:
   a second bug that the first one was hiding.
4. Two regression guards in `tests/test_repo_hygiene.py`:
   `test_no_source_file_is_git_ignored`, which feeds every source file
   through `git check-ignore --stdin`, and `test_every_agent_folder_is_tracked`,
   which compares folders on disk against what git will ship.

## Status

Fixed. Both guards verified against the original bug: restoring the
unanchored pattern makes them fail and name `agents/build/manifest.yaml`
and `agents/build/agent.md` explicitly.
