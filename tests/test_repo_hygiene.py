"""Repo-level invariants that no amount of unit testing would catch.

The bug that prompted this file: .gitignore carried an unanchored "build/"
for Python build artefacts, which also matched agents/build/. The shipped
build agent was therefore never committed, the loader found four agents
instead of five, and CI stayed red on a file nobody could see in a diff.

Nothing here needs the network; the git checks skip cleanly when the tests
run from an exported tarball rather than a working repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Directories whose contents are source: if git is ignoring anything in
# here, something is being shipped in a working copy and nowhere else.
SOURCE_DIRS = ("agents", "alfred", "tests", "docs", "config", "scripts", ".github")


def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        input=stdin,
    )


@pytest.fixture(scope="module")
def git_repo() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git working tree")
    if _git("--version").returncode != 0:
        pytest.skip("git is not available")
    return


@pytest.mark.usefixtures("git_repo")
def test_no_source_file_is_git_ignored() -> None:
    """Every file under a source directory is visible to git.

    check-ignore exits 0 when it matched something, so a non-empty stdout is
    the failure: those paths exist on disk and would vanish from a clone.
    """
    candidates = [
        path
        for directory in SOURCE_DIRS
        for path in (REPO_ROOT / directory).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    relative = [p.relative_to(REPO_ROOT).as_posix() for p in candidates]
    # agents/<name>/state/ is owner runtime state and is ignored on purpose.
    relative = [p for p in relative if "/state/" not in p]

    result = _git("check-ignore", "--stdin", stdin="\n".join(relative))
    ignored = [line for line in result.stdout.splitlines() if line.strip()]

    assert not ignored, (
        "these tracked-looking source files are invisible to git, so they "
        "exist in this working copy and in no clone of it. Anchor the "
        ".gitignore pattern that swallows them (a leading '/' scopes it to "
        "the repo root):\n" + "\n".join(ignored)
    )


@pytest.mark.usefixtures("git_repo")
def test_every_agent_folder_is_tracked() -> None:
    """The loader reads folders; git ships files. They must agree.

    --others --exclude-standard counts a not-yet-committed agent as shippable,
    so writing a new agent does not fail this test; only one git would refuse
    to see does.
    """
    on_disk = {p.name for p in AGENTS_DIR.iterdir() if p.is_dir()}
    listing = _git("ls-files", "--cached", "--others", "--exclude-standard", "--", "agents")
    tracked = {
        line.split("/")[1]
        for line in listing.stdout.splitlines()
        if line.startswith("agents/") and "/" in line[len("agents/") :]
    }

    assert on_disk <= tracked, (
        "agent folders present on disk but absent from git: "
        f"{sorted(on_disk - tracked)}. A clone would load fewer agents than "
        "this working copy does."
    )


def test_every_agent_folder_has_a_manifest_and_a_prompt() -> None:
    """A half-written agent folder should fail here, not at owner startup."""
    problems = []
    for folder in sorted(p for p in AGENTS_DIR.iterdir() if p.is_dir()):
        manifest = folder / "manifest.yaml"
        prompt = folder / "agent.md"
        if not manifest.is_file():
            problems.append(f"{folder.name}: no manifest.yaml")
            continue
        if not prompt.is_file() or not prompt.read_text(encoding="utf-8").strip():
            problems.append(f"{folder.name}: missing or empty agent.md")
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            problems.append(f"{folder.name}: manifest.yaml is not a mapping")
            continue
        if data.get("name") != folder.name:
            problems.append(
                f"{folder.name}: manifest name is {data.get('name')!r}; the "
                "folder name and the manifest name must match"
            )
        if not data.get("allowed_tools"):
            problems.append(f"{folder.name}: empty allowed_tools")

    assert not problems, "\n".join(problems)


def test_shipped_agents_leave_room_for_the_builder() -> None:
    """The builder must be able to build something on a fresh install.

    Every active agent's capacity_cost counts against the owner's weekly
    capacity, and the builder refuses a blueprint that pushes the total over
    it. If the shipped fleet already spends the default budget, `new agent`
    fails out of the box, which would make the headline feature dead on
    arrival for anyone who never edits their profile.
    """
    default_weekly_capacity = 20
    smallest_buildable_habit = 1

    total = 0
    for folder in sorted(p for p in AGENTS_DIR.iterdir() if p.is_dir()):
        data = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        if data.get("lifecycle", "established") in {"paused", "retired"}:
            continue
        total += int(data.get("capacity_cost", 0))

    assert total + smallest_buildable_habit <= default_weekly_capacity, (
        f"shipped agents claim {total} of {default_weekly_capacity} weekly "
        "capacity points, leaving no room for the builder to add even the "
        "smallest habit on a fresh install"
    )
