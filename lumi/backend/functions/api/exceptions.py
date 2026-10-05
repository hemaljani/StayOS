"""LUMI API custom exception classes.

Provides domain-specific exceptions that map to HTTP status codes
for consistent error response formatting across all API handlers.
"""

from typing import Optional


class ApiError(Exception):
    """Base exception for all LUMI API errors.

    Attributes:
        status_code: HTTP status code to return.
        error_code: Machine-readable error code string.
        message: Human-readable error description.
        field: Optional field name for validation errors.
    """

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        field: Optional[str] = None,
    ) -> None:
        """Initialize ApiError.

        Args:
            status_code: HTTP status code (e.g., 400, 401, 404, 500).
            error_code: Machine-readable code (e.g., PROPERTY_NOT_FOUND).
            message: Human-readable error description.
            field: Optional field name for validation errors.
        """
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.field = field

    def to_response_body(self) -> dict:
        """Convert exception to API error response body.

        Returns:
            Dictionary matching the standard error response format.
        """
        error_body: dict = {
            "code": self.error_code,
            "message": self.message,
        }
        if self.field:
            error_body["field"] = self.field
        return {"error": error_body}


class BriefNotFoundException(ApiError):
    """Raised when no brief exists for the requested property."""

    def __init__(self, property_id: str) -> None:
        """Initialize with the property ID that was not found.

        Args:
            property_id: The property ID that has no brief.
        """
        super().__init__(
            status_code=404,
            error_code="PROPERTY_NOT_FOUND",
            message=f"No brief found for property {property_id}",
        )


class SettingsNotFoundException(ApiError):
    """Raised when no settings exist for the requested GM."""

    def __init__(self, gm_alias: str) -> None:
        """Initialize with the GM alias that was not found.

        Args:
            gm_alias: The GM alias that has no settings.
        """
        super().__init__(
            status_code=404,
            error_code="SETTINGS_NOT_FOUND",
            message=f"No settings found for GM {gm_alias}",
        )


class UnauthorizedError(ApiError):
    """Raised when authentication fails or token is invalid."""

    def __init__(self, message: str = "Invalid or expired token") -> None:
        """Initialize with optional custom message.

        Args:
            message: Human-readable unauthorized description.
        """
        super().__init__(
            status_code=401,
            error_code="UNAUTHORIZED",
            message=message,
        )


class ForbiddenError(ApiError):
    """Raised when user does not have access to the requested resource."""

    def __init__(self, message: str = "Access denied to this resource") -> None:
        """Initialize with optional custom message.

        Args:
            message: Human-readable forbidden description.
        """
        super().__init__(
            status_code=403,
            error_code="FORBIDDEN",
            message=message,
        )


class ValidationError(ApiError):
    """Raised when request input fails validation."""

    def __init__(self, message: str, field: Optional[str] = None) -> None:
        """Initialize with validation error details.

        Args:
            message: Description of what validation failed.
            field: Optional field name that failed validation.
        """
        super().__init__(
            status_code=400,
            error_code="VALIDATION_ERROR",
            message=message,
            field=field,
        )


class InternalError(ApiError):
    """Raised for unexpected server-side failures."""

    def __init__(self, message: str = "An internal error occurred") -> None:
        """Initialize with optional custom message.

        Args:
            message: Human-readable internal error description.
        """
        super().__init__(
            status_code=500,
            error_code="INTERNAL_ERROR",
            message=message,
        )
