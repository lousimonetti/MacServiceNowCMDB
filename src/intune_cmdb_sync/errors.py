"""Exception hierarchy for the connector."""

from __future__ import annotations


class SyncError(Exception):
    """Base class for all connector errors."""


class ConfigError(SyncError):
    """Raised when configuration is missing or internally inconsistent."""


class AuthError(SyncError):
    """Raised when a token could not be acquired for Graph or ServiceNow."""


class GraphError(SyncError):
    """Raised for non-retryable Microsoft Graph failures."""


class ServiceNowError(SyncError):
    """Raised for non-retryable ServiceNow failures."""


class RetryableHTTPError(SyncError):
    """Internal signal that a request should be retried.

    Carries the number of seconds the server asked us to wait, when it said so.
    """

    def __init__(self, message: str, *, status_code: int, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SafetyLimitExceeded(SyncError):
    """Raised when a guardrail (e.g. mass-retirement threshold) trips."""
