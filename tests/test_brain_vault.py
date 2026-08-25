"""Link integrity for the brain vault.

The vault in brain/ is the standards research, the decision records, and a
live audit of this repository against them. It is cross-linked heavily, and
an Obsidian wikilink to a note that does not exist fails silently: the link
renders, it just goes nowhere. A vault whose links rot stops being read.

These tests need no network and no Obsidian; they are plain text checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT = REPO_ROOT / "brain"

# [[Note]] and [[Note|shown text]], excluding embeds (![[...]]).
WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")

# Wikilinks that appear inside a template's instructional text rather than
# pointing at a real note.
TEMPLATE_PLACEHOLDERS = {"..."}


def _notes() -> list[Path]:
    return sorted(VAULT.rglob("*.md"))


def _note_names() -> set[str]:
    return {p.stem for p in _notes()}


@pytest.fixture(scope="module")
def vault() -> None:
    if not VAULT.is_dir():
        pytest.skip("no brain vault in this checkout")
    return


@pytest.mark.usefixtures("vault")
def test_every_wikilink_resolves() -> None:
    """A link to a missing note renders fine and goes nowhere."""
    known = _note_names()
    broken = []
    for note in _notes():
        for match in WIKILINK.finditer(note.read_text(encoding="utf-8")):
            target = match.group(1).strip()
            if target in TEMPLATE_PLACEHOLDERS or target in known:
                continue
            line = note.read_text(encoding="utf-8")[: match.start()].count("\n") + 1
            broken.append(
                f"{note.relative_to(REPO_ROOT).as_posix()}:{line} -> [[{target}]]"
            )

    assert not broken, (
        "these wikilinks point at notes that do not exist. Obsidian renders "
        "them without complaint, so nothing else would catch this:\n"
        + "\n".join(broken)
    )


@pytest.mark.usefixtures("vault")
def test_no_note_is_orphaned() -> None:
    """Every note is reachable from another note.

    An unlinked note is one nobody will find. The maps in 00-maps/ are the
    entry points and are exempt, as are the templates, which are copied
    rather than linked.
    """
    linked: set[str] = set()
    for note in _notes():
        for match in WIKILINK.finditer(note.read_text(encoding="utf-8")):
            linked.add(match.group(1).strip())

    orphans = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in _notes()
        if p.stem not in linked
        and p.parent.name not in {"00-maps", "90-templates"}
        and p.name != "README.md"
    ]

    assert not orphans, (
        "these notes are linked from nowhere, so nobody will find them. "
        "Link them from a map in 00-maps/ or from a related note:\n"
        + "\n".join(orphans)
    )


@pytest.mark.usefixtures("vault")
def test_every_standard_note_states_its_verification() -> None:
    """A standard with no verification section is an opinion.

    The vault's own rule, from brain/README.md: if a standard cannot be
    checked, it must say so rather than implying it holds.
    """
    missing = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted((VAULT / "10-standards").glob("*.md"))
        if "## Verification" not in p.read_text(encoding="utf-8")
    ]

    assert not missing, (
        "every standard note needs a Verification section naming the "
        "command, test, or CI job that proves it, or saying plainly that "
        "nothing does:\n" + "\n".join(missing)
    )
