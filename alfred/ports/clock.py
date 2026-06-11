"""ClockPort: time as a dependency.

Scheduling, lifecycle cadences, and adaptation all reason about time.
Injecting the clock keeps the domain deterministic under test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    """Wall-clock time and cooperative sleep."""

    def now(self) -> datetime:
        """Current time, always timezone-aware (local timezone)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Yield for the given duration. Fake clocks may return instantly."""
        ...
