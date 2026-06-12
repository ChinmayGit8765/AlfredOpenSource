"""ALFRED's terminal identity: one console, one theme, a few renderers.

The terminal is ALFRED's face, so it gets the same care as the brain.
Gold on dark: a butler's livery, not a neon dashboard. Everything here
is presentation only; alongside runtime/cli.py this is the only module
allowed to write to the terminal.
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

import alfred
from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import Lifecycle

_THEME = Theme(
    {
        "brand": "bold #f5c542",
        "accent": "#d7a13b",
        "chrome": "grey58",
        "ok": "green3",
        "warn": "yellow3",
        "bad": "red3",
        "owner": "bold white",
    }
)

console = Console(theme=_THEME, highlight=False)


def _can_encode(sample: str) -> bool:
    """Whether the attached console can actually write these glyphs.

    Legacy Windows consoles encode to a codepage like cp1252 on write, so
    a glyph the codepage lacks is a crash, not a substitution. Everything
    decorative therefore has an ASCII twin.
    """
    encoding = getattr(console.file, "encoding", None) or "ascii"
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


FANCY = _can_encode("█✓✗❯·")

_BANNER_FANCY = (
    " █████  ██      ███████ ██████  ███████ ██████ ",
    "██   ██ ██      ██      ██   ██ ██      ██   ██",
    "███████ ██      █████   ██████  █████   ██   ██",
    "██   ██ ██      ██      ██   ██ ██      ██   ██",
    "██   ██ ███████ ██      ██   ██ ███████ ██████ ",
)

_BANNER_PLAIN = (
    " #####  ##      ####### ######  ####### ###### ",
    "##   ## ##      ##      ##   ## ##      ##   ##",
    "####### ##      #####   ######  #####   ##   ##",
    "##   ## ##      ##      ##   ## ##      ##   ##",
    "##   ## ####### ##      ##   ## ####### ###### ",
)

_BANNER_ROWS = _BANNER_FANCY if FANCY else _BANNER_PLAIN

# Top-to-bottom gold fade: lit from above, like a reading lamp.
_BANNER_FADE = ("#ffe28a", "#f7d05e", "#eebf45", "#dca83a", "#c4922f")

DOT = "·" if FANCY else "-"
_DOT = DOT
TAGLINE = f"self-hosted {_DOT} local-first {_DOT} one loyalty: yours"

_LIFECYCLE_STYLES: dict[Lifecycle, str] = {
    Lifecycle.PROPOSED: "grey62",
    Lifecycle.FORMING: "yellow3",
    Lifecycle.ESTABLISHED: "green3",
    Lifecycle.MAINTENANCE: "cyan",
    Lifecycle.LAPSING: "red3",
    Lifecycle.RESHAPED: "orange3",
    Lifecycle.PAUSED: "grey50",
    Lifecycle.RETIRED: "grey35",
}


def lifecycle_text(state: Lifecycle) -> Text:
    return Text(state.value, style=_LIFECYCLE_STYLES.get(state, "white"))


def print_banner(subtitle: str = "") -> None:
    """The masthead: banner, tagline, optional mode line."""
    console.print()
    for row, colour in zip(_BANNER_ROWS, _BANNER_FADE):
        console.print(Text(row, style=colour), justify="left")
    meta = Text()
    meta.append(f"v{alfred.__version__}", style="accent")
    meta.append(f"  {TAGLINE}", style="chrome")
    if subtitle:
        meta.append(f"  {_DOT}  {subtitle}", style="chrome")
    console.print(meta)
    console.print()


def print_reply(text: str) -> None:
    """One message from ALFRED: a gold-edged panel with markdown inside.

    Legacy consoles get plain text: rich's markdown bullets and rules use
    glyphs an old codepage cannot encode.
    """
    body = Markdown(text) if FANCY else Text(text)
    console.print(
        Panel(
            body,
            title="ALFRED",
            title_align="left",
            border_style="accent",
            padding=(0, 1),
        )
    )


def prompt_string() -> str:
    """The owner's input prompt; raw ANSI only when a real terminal listens.

    The prompt is printed by input() on the stdin thread, outside rich's
    control, so styling has to be a literal escape sequence.
    """
    if console.is_terminal and FANCY:
        return "\x1b[1;37myou ❯\x1b[0m "
    return "you> "


def agents_table(agents: list[LoadedAgent]) -> Table:
    table = Table(
        title="Agents",
        title_style="brand",
        border_style="chrome",
        header_style="accent",
    )
    table.add_column("name", style="owner")
    table.add_column("lifecycle")
    table.add_column("domain", style="chrome")
    table.add_column("shape", style="chrome")
    table.add_column("schedule", style="chrome")
    table.add_column("tools", justify="right", style="chrome")
    for agent in agents:
        manifest = agent.manifest
        schedule = manifest.schedule
        when = schedule.kind
        if schedule.kind in ("daily", "weekly") and schedule.time:
            days = ",".join(schedule.days) + " " if schedule.days else ""
            when = f"{schedule.kind} {days}{schedule.time}"
        elif schedule.kind == "interval" and schedule.every_minutes:
            when = f"every {schedule.every_minutes}m"
        table.add_row(
            manifest.name,
            lifecycle_text(manifest.lifecycle),
            manifest.domain or "-",
            manifest.shape.value if manifest.shape else "-",
            when,
            str(len(manifest.allowed_tools)),
        )
    return table


def check_line(state: str, label: str, detail: str = "") -> Text:
    """One doctor row: ok / warn / bad / off with a uniform layout."""
    marks = (
        {"ok": ("✓", "ok"), "warn": ("!", "warn"), "bad": ("✗", "bad"), "off": ("-", "chrome")}
        if FANCY
        else {"ok": ("+", "ok"), "warn": ("!", "warn"), "bad": ("x", "bad"), "off": ("-", "chrome")}
    )
    mark, style = marks[state]
    line = Text()
    line.append(f" {mark} ", style=style)
    line.append(f"{label:<18}", style="owner")
    if detail:
        line.append(detail, style="chrome")
    return line


def step(text: str) -> None:
    """A quiet progress line for startup sequences."""
    console.print(Text(f"  {_DOT} ", style="accent") + Text(text, style="chrome"))
