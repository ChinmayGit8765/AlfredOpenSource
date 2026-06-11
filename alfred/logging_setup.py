"""Structured logging for ALFRED.

Stdlib logging with a key=value formatter; modules log via
logging.getLogger(__name__). No print debugging in committed code.
Secrets must never reach a log call.
"""

from __future__ import annotations

import logging
import sys


class _KeyValueFormatter(logging.Formatter):
    """Compact structured lines: time level logger message extras."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} "
            f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        )
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(level: int = logging.INFO) -> None:
    """Install ALFRED's log handler once, idempotently."""
    root = logging.getLogger()
    if any(getattr(h, "_alfred", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_KeyValueFormatter())
    handler._alfred = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    # Library noise stays at WARNING so ALFRED's own signal is readable.
    for noisy in ("discord", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
