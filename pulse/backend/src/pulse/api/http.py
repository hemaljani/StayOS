"""HTTP request parsing and JSON response helpers for the PULSE REST API.

The ``pulse-api`` Lambda runs behind API Gateway. This module normalizes the two
proxy-integration event shapes PULSE may receive (HTTP API payload format v2 and
REST API proxy) into a single :class:`Request` value, and builds the proxy
response envelope every route returns. Keeping this parsing/serialization in one
pure module lets the router and per-resource handlers stay thin and testable.

Design points:
    * **Stage-prefix tolerant path parsing.** The normalized path is split into
      segments and, when the first segment is not a known API resource, a
      leading stage segment (e.g. ``/v1``) is dropped so routing matches
      regardless of the deployed stage.
    * **DynamoDB-friendly JSON.** ``pulse-alerts`` items read through the boto3
      resource interface contain :class:`~decimal.Decimal` numbers; the response
      encoder converts them to ``int``/``float`` so the body serializes cleanly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

# The first path segment of every PULSE API resource. Used to detect and strip a
# leading stage segment when the deployment stage is included in the raw path.
_KNOWN_RESOURCES = frozenset(
    {"alerts", "shift-handover", "rules", "push-subscriptions", "config", "demo", "kitchen"}
)

# Standard JSON content type header applied to every response.
_JSON_HEADERS = {"content-type": "application/json"}


@dataclass(frozen=True)
class Request:
    """A normalized view of an API Gateway proxy request.

    Attributes:
        method: The HTTP method in upper case (e.g. ``GET``, ``POST``).
        segments: The path split into non-empty segments with any leading stage
            segment removed (e.g. ``["alerts", "alert-1", "approvals"]``).
        query: The query-string parameters (never ``None``).
        body: The parsed JSON body as a mapping, or an empty dict when the body
            is absent or not a JSON object.
        raw_path: The original raw path, retained for logging.
    """

    method: str
    segments: list[str]
    query: dict[str, str]
    body: dict[str, Any]
    raw_path: str = ""

    def segment(self, index: int) -> Optional[str]:
        """Return the path segment at ``index``, or ``None`` when absent.

        Args:
            index: The 0-based segment index.

        Returns:
            The segment value, or ``None`` when out of range.
        """
        if 0 <= index < len(self.segments):
            return self.segments[index]
        return None


def _method(event: Mapping[str, Any]) -> str:
    """Resolve the HTTP method from either event shape.

    Args:
        event: The API Gateway invocation event.

    Returns:
        The upper-cased HTTP method, or an empty string when absent.
    """
    context = event.get("requestContext", {})
    if isinstance(context, Mapping):
        http = context.get("http")
        if isinstance(http, Mapping) and http.get("method"):
            return str(http["method"]).upper()
    if event.get("httpMethod"):
        return str(event["httpMethod"]).upper()
    return ""


def _raw_path(event: Mapping[str, Any]) -> str:
    """Resolve the request path from either event shape.

    Args:
        event: The API Gateway invocation event.

    Returns:
        The request path (defaults to ``/`` when absent).
    """
    if event.get("rawPath"):
        return str(event["rawPath"])
    context = event.get("requestContext", {})
    if isinstance(context, Mapping):
        http = context.get("http")
        if isinstance(http, Mapping) and http.get("path"):
            return str(http["path"])
    if event.get("path"):
        return str(event["path"])
    return "/"


def _split_path(raw_path: str) -> list[str]:
    """Split a raw path into segments, dropping a leading stage segment.

    Args:
        raw_path: The raw request path.

    Returns:
        The non-empty path segments with any leading stage segment removed.
    """
    segments = [segment for segment in raw_path.split("/") if segment]
    # If the first segment is not a known resource but the second is, the first
    # is a stage prefix (e.g. "/v1/alerts") and is dropped.
    if (
        len(segments) >= 2
        and segments[0] not in _KNOWN_RESOURCES
        and segments[1] in _KNOWN_RESOURCES
    ):
        return segments[1:]
    return segments


def _parse_body(event: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the request JSON body into a mapping.

    Args:
        event: The API Gateway invocation event.

    Returns:
        The parsed JSON object, or an empty dict when absent or not an object.
    """
    raw_body = event.get("body")
    if not raw_body:
        return {}
    if isinstance(raw_body, Mapping):
        return dict(raw_body)
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_request(event: Mapping[str, Any]) -> Request:
    """Parse an API Gateway proxy event into a normalized :class:`Request`.

    Args:
        event: The API Gateway invocation event (HTTP API v2 or REST proxy).

    Returns:
        The normalized request.
    """
    query = event.get("queryStringParameters") or {}
    raw_path = _raw_path(event)
    return Request(
        method=_method(event),
        segments=_split_path(raw_path),
        query={str(key): str(value) for key, value in query.items()},
        body=_parse_body(event),
        raw_path=raw_path,
    )


def _decimal_default(value: Any) -> Any:
    """JSON encoder default that renders ``Decimal`` as ``int``/``float``.

    Args:
        value: The value ``json.dumps`` could not natively serialize.

    Returns:
        A JSON-serializable representation.

    Raises:
        TypeError: If the value is not a ``Decimal`` (delegated to the default
            encoder behavior).
    """
    if isinstance(value, Decimal):
        # Render integers without a trailing ".0"; keep fractional values float.
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_response(status_code: int, body: Any) -> dict[str, Any]:
    """Build an API Gateway proxy JSON response envelope.

    Args:
        status_code: The HTTP status code.
        body: The response body (serialized to JSON; Decimals handled).

    Returns:
        A proxy response dict with ``statusCode``, ``headers``, and ``body``.
    """
    return {
        "statusCode": status_code,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(body, default=_decimal_default),
    }


def error_response(status_code: int, message: str, **extra: Any) -> dict[str, Any]:
    """Build a JSON error response envelope.

    Args:
        status_code: The HTTP status code.
        message: A human-readable error message.
        **extra: Additional fields to include in the error body.

    Returns:
        A proxy response dict carrying an ``error`` object.
    """
    payload: dict[str, Any] = {"error": {"message": message}}
    if extra:
        payload["error"].update(extra)
    return json_response(status_code, payload)


__all__ = [
    "Request",
    "parse_request",
    "json_response",
    "error_response",
]
