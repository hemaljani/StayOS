"""Domain-specific exception hierarchy for PULSE.

Following the project PYQUALITY rules, PULSE defines project-specific exception
classes for domain errors rather than reusing generic ``ValueError`` /
``RuntimeError``. All PULSE exceptions derive from ``PulseError`` so callers can
catch the whole family with a single ``except`` when that is the actionable
choice, while still being able to catch a specific subtype.

Exceptions carry structured context (identifiers, stage) so handlers can log
with enough detail to diagnose the failure before re-raising or converting it
to a graceful-degradation path (see ``design.md`` Error Handling).
"""

from __future__ import annotations

from typing import Optional


class PulseError(Exception):
    """Base class for all PULSE domain errors.

    Attributes:
        message: Human-readable description of the failure.
    """

    def __init__(self, message: str) -> None:
        """Initialize the base error.

        Args:
            message: Human-readable description of the failure.
        """
        super().__init__(message)
        self.message = message


class ConfigurationError(PulseError):
    """Raised when required configuration is missing or invalid.

    Typically raised by the environment-variable config loader when a required
    resource identifier (table name, model id) is absent, so a Lambda fails
    fast at cold start rather than mid-request.

    Attributes:
        variable: The offending environment variable name, if known.
    """

    def __init__(self, message: str, variable: Optional[str] = None) -> None:
        """Initialize the configuration error.

        Args:
            message: Human-readable description of the misconfiguration.
            variable: The offending environment variable name, if known.
        """
        super().__init__(message)
        self.variable = variable


class RuleEvaluationError(PulseError):
    """Raised when a rule trigger condition cannot be evaluated.

    Per Requirement 1.6, the Rule Engine records an evaluation error that
    identifies the affected event and rule, then continues processing
    subsequent events. This exception carries that context.

    Attributes:
        rule_type: The rule type being evaluated, if known.
        detail: Additional context (e.g. the missing source field).
    """

    def __init__(
        self,
        message: str,
        rule_type: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        """Initialize the rule-evaluation error.

        Args:
            message: Human-readable description of the failure.
            rule_type: The rule type being evaluated, if known.
            detail: Additional context, such as the missing source field.
        """
        super().__init__(message)
        self.rule_type = rule_type
        self.detail = detail


class TriageFailure(PulseError):
    """Raised when the Triage Agent cannot produce a valid triage brief.

    Covers Bedrock invocation errors, timeouts (Requirements 1.7, 10.6), and
    malformed/schema-invalid model output (Property 18 guards well-formedness
    before persistence). The Rule Engine treats this as a signal to deliver the
    alert without a brief and record a triage-failure event.

    Attributes:
        alert_id: The affected alert identifier, if known.
        reason: Machine-friendly failure reason (e.g. "timeout",
            "invalid_json", "bedrock_error").
    """

    def __init__(
        self,
        message: str,
        alert_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Initialize the triage failure.

        Args:
            message: Human-readable description of the failure.
            alert_id: The affected alert identifier, if known.
            reason: Machine-friendly failure reason.
        """
        super().__init__(message)
        self.alert_id = alert_id
        self.reason = reason


class WriteBackError(PulseError):
    """Raised when the Action Executor's operational write-back fails.

    Per Requirement 12.6 the write-back and the RESOLVED update share a single
    transaction, so a failure leaves the alert unchanged with no resolution
    timestamp. This exception is surfaced to the API caller so the PWA can show
    "the option action could not be completed".

    Attributes:
        alert_id: The affected alert identifier, if known.
        detail: Additional context about the failed mutation.
    """

    def __init__(
        self,
        message: str,
        alert_id: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        """Initialize the write-back error.

        Args:
            message: Human-readable description of the failure.
            alert_id: The affected alert identifier, if known.
            detail: Additional context about the failed mutation.
        """
        super().__init__(message)
        self.alert_id = alert_id
        self.detail = detail


class OpsReadFailure(PulseError):
    """Raised when the C3b VIPs/Ops facade cannot read/shape Gateway data.

    The ``pulse-ops-read`` facade (Component 5, Decision 9) is an MCP client to
    the shared StayOS AgentCore Gateway. When a Gateway connection cannot be
    opened, a tool invocation fails, or a tool reports an unavailable result,
    the facade raises this so the API boundary can return a clean 5xx error
    envelope (never a crash) for the VIPs/Ops PWA tabs.

    Attributes:
        tool: The Gateway tool name involved, if known.
        reason: Machine-friendly failure reason (e.g. ``"gateway_connect_error"``,
            ``"gateway_tool_error"``, ``"gateway_tool_unavailable"``).
    """

    def __init__(
        self,
        message: str,
        tool: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Initialize the ops-read failure.

        Args:
            message: Human-readable description of the failure.
            tool: The Gateway tool name involved, if known.
            reason: Machine-friendly failure reason.
        """
        super().__init__(message)
        self.tool = tool
        self.reason = reason


__all__ = [
    "PulseError",
    "ConfigurationError",
    "RuleEvaluationError",
    "TriageFailure",
    "WriteBackError",
    "OpsReadFailure",
]
