"""ALFRED's exception hierarchy.

Every error raised by ALFRED's own code derives from AlfredError so callers
can distinguish system faults from library and OS errors at the boundary.
"""

from __future__ import annotations


class AlfredError(Exception):
    """Base class for all errors raised by ALFRED."""


class ConfigError(AlfredError):
    """Configuration is missing, malformed, or inconsistent."""


class ManifestError(AlfredError):
    """An agent manifest failed validation or its folder is malformed."""


class StructuredCallError(AlfredError):
    """The model never produced output matching the required schema.

    Raised only after the bounded retry loop is exhausted. Carries the
    last validation error text so callers can log a useful reason.
    """

    def __init__(self, message: str, *, attempts: int = 0, last_error: str = "") -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class ToolNotFoundError(AlfredError):
    """The requested tool does not exist in any connected tool source."""


class ToolNotAllowedError(AlfredError):
    """An agent attempted to invoke a tool absent from its allowlist.

    This is a governance violation, not a user-facing failure: the
    dispatcher refuses and audits, it never silently widens access.
    """


class StoreError(AlfredError):
    """The persistence layer failed in a way the adapter could not recover."""


class TransportError(AlfredError):
    """An outbound message could not be delivered."""
