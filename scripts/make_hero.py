"""Render the README hero image: a staged ALFRED session exported as SVG.

Rich records its own output, so the hero is a pixel-perfect rendering of
the real UI theme rather than a screenshot. Regenerate after UI changes:

    .venv/Scripts/python.exe scripts/make_hero.py
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

THEME = Theme(
    {
        "brand": "bold #f5c542",
        "accent": "#d7a13b",
        "chrome": "grey58",
        "ok": "green3",
        "owner": "bold white",
    }
)

BANNER = (
    " █████  ██      ███████ ██████  ███████ ██████ ",
    "██   ██ ██      ██      ██   ██ ██      ██   ██",
    "███████ ██      █████   ██████  █████   ██   ██",
    "██   ██ ██      ██      ██   ██ ██      ██   ██",
    "██   ██ ███████ ██      ██   ██ ███████ ██████ ",
)
FADE = ("#ffe28a", "#f7d05e", "#eebf45", "#dca83a", "#c4922f")

PLAN = """\
Deload week confirmed. You reported the shoulder flare on Tuesday, so volume
drops 40% and nothing overhead until it settles.

| day | session | load |
|-----|---------|------|
| mon | Easy spin, 30 min, zone 1 | 1 |
| wed | Lower only: squat 3x5 @ 70%, no press | 2 |
| fri | Climb, juggy 6As, stop before pump | 2 |

Anchor: straight after your 8am coffee, kit is already by the door.
One miss is fine. Tell me what happened in one line and I adjust."""


def main() -> None:
    # Render into a buffer, never the real console: the local terminal's
    # codepage must not influence what the SVG contains.
    console = Console(
        theme=THEME, record=True, width=84, force_terminal=True, file=io.StringIO()
    )

    console.print()
    for row, colour in zip(BANNER, FADE, strict=True):
        console.print(Text(row, style=colour))
    console.print(
        Text("v0.1.0", style="accent")
        + Text("  self-hosted · local-first · one loyalty: yours", style="chrome")
        + Text("  ·  chat · qwen3:8b", style="chrome")
    )
    console.print()
    console.print(Text("you ❯ ", style="owner") + Text("I'm flaring up, deload this week"))
    console.print()
    console.print(
        Panel(
            Markdown(PLAN),
            title="ALFRED",
            title_align="left",
            border_style="accent",
            padding=(0, 1),
        )
    )
    console.print(
        Text("  ✓ ", style="ok")
        + Text("plan validated against schema, stored, adherence tracking armed", style="chrome")
    )
    console.print()

    out = Path(__file__).resolve().parent.parent / "docs" / "assets" / "terminal.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(console.export_svg(title="alfred"), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
