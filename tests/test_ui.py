"""Tests for the pure terminal renderers in runtime/ui.py.

Presentation only, no TTY: each renderer returns a rich object whose text
content we can assert without touching a real console.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from alfred.domain.registry import LoadedAgent
from alfred.domain.schemas import AgentManifest, Lifecycle, Schedule, TargetShape
from alfred.runtime import ui


def column_cells(table: Table, index: int) -> list[str]:
    """The plain text of one column's cells, independent of theme/width."""
    return [
        cell.plain if isinstance(cell, Text) else str(cell)
        for cell in table.columns[index].cells
    ]


def test_lifecycle_text_carries_the_state_name() -> None:
    text = ui.lifecycle_text(Lifecycle.LAPSING)
    assert isinstance(text, Text)
    assert text.plain == "lapsing"


def test_check_line_includes_label_and_detail() -> None:
    line = ui.check_line("ok", "ollama", "reachable")
    assert isinstance(line, Text)
    assert "ollama" in line.plain
    assert "reachable" in line.plain


def test_check_line_supports_every_state() -> None:
    for state in ("ok", "warn", "bad", "off"):
        assert "label" in ui.check_line(state, "label").plain


def test_agents_table_has_one_row_per_agent_with_schedule_and_tool_count() -> None:
    agents = [
        LoadedAgent(
            manifest=AgentManifest(
                name="training",
                description="d",
                shape=TargetShape.SKILL,
                schedule=Schedule(kind="weekly", days=["mon"], time="08:00"),
                allowed_tools=["a", "b"],
            ),
            prompt="p",
        ),
        LoadedAgent(
            manifest=AgentManifest(
                name="reading",
                description="d",
                shape=TargetShape.HABIT,
                schedule=Schedule(kind="daily", time="21:00"),
            ),
            prompt="p",
        ),
    ]
    table = ui.agents_table(agents)
    assert isinstance(table, Table)
    assert table.row_count == 2

    assert column_cells(table, 0) == ["training", "reading"]  # names
    assert column_cells(table, 4) == ["weekly mon 08:00", "daily 21:00"]  # schedule
    assert column_cells(table, 5) == ["2", "0"]  # tool counts
