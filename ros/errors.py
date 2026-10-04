"""Typed error taxonomy. Failures are recorded, never disguised as empty content."""

from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    INVALID_CREDENTIALS = "invalid_credentials"
    NO_BALANCE = "no_balance"
    RATE_LIMITED = "rate_limited"
    PLATFORM_BLOCKED = "platform_blocked"
    CONTENT_DELETED = "content_deleted"
    TRANSIENT = "transient"
    SOURCE_UNREACHABLE = "source_unreachable"
    UNSUPPORTED_FORMAT = "unsupported_format"
    EXTRACTION_INCOMPLETE = "extraction_incomplete"
    EMPTY_RESPONSE = "empty_response"
    MODEL_ERROR = "model_error"
    POLICY_REJECTED = "policy_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERNAL = "internal"


RETRYABLE = {ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED}
# Errors that make continuing pointless: every further call would fail the same way.
FATAL = {ErrorKind.INVALID_CREDENTIALS, ErrorKind.NO_BALANCE}


class RosError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL

    def __str__(self) -> str:
        return f"[{self.kind.value}] {self.message}"


class BudgetExhausted(RosError):
    def __init__(self, limit: str, message: str):
        super().__init__(ErrorKind.BUDGET_EXHAUSTED, message)
        self.limit = limit


class NeedsUserAction(Exception):
    """Raised when ROS must stop and ask: credentials, approvals, ambiguous config."""
