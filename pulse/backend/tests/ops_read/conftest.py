"""Shared fixtures and fakes for the C3b VIPs/Ops facade tests.

Every external boundary (the shared Gateway MCP tool caller) is faked here, so
the tests never open a network connection or import ``mcp_proxy_for_aws`` /
``strands``. The :class:`RecordingToolCaller` mirrors the Triage Agent tests'
seam style: it records the ``(tool_name, arguments)`` calls and returns canned
results in the real shared-tool ``{status, data}`` envelope.
"""

from __future__ import annotations

from typing import Any

from pulse.api.identity import CallerIdentity


class RecordingToolCaller:
    """A fake Gateway tool caller that records calls and returns canned results.

    Attributes:
        results: Mapping of tool name -> canned JSON result to return.
        calls: The recorded ``(tool_name, arguments)`` tuples, in call order.
    """

    def __init__(self, results: dict[str, Any]) -> None:
        """Initialize with a mapping of tool name -> canned result."""
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Record the call and return the canned result for ``tool_name``."""
        self.calls.append((tool_name, dict(arguments)))
        if tool_name not in self.results:
            raise AssertionError(f"unexpected tool call: {tool_name}")
        return self.results[tool_name]

    def property_ids(self) -> list[Any]:
        """Return the ``propertyId`` argument passed to every recorded call."""
        return [args.get("propertyId") for _name, args in self.calls]


class RaisingToolCaller:
    """A fake tool caller that raises to simulate a Gateway/tool failure.

    Attributes:
        error: The exception instance to raise on every call.
        calls: The recorded call names (to assert it was reached).
    """

    def __init__(self, error: Exception) -> None:
        """Initialize with the exception to raise."""
        self.error = error
        self.calls: list[str] = []

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Record the call then raise the configured error."""
        self.calls.append(tool_name)
        raise self.error


def ok(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a data mapping in the shared tool's success envelope.

    Args:
        data: The tool's data payload.

    Returns:
        ``{"status": "success", "data": data}``.
    """
    return {"status": "success", "data": data}


def unavailable(message: str = "temporarily unavailable") -> dict[str, Any]:
    """Build the shared tool's unavailable envelope.

    Args:
        message: The unavailability message.

    Returns:
        ``{"status": "unavailable", "message": message}``.
    """
    return {"status": "unavailable", "message": message}


def make_identity(gm_alias: str, properties: set[str]) -> CallerIdentity:
    """Build a :class:`CallerIdentity` for tests.

    Args:
        gm_alias: The caller identity.
        properties: The associated property set.

    Returns:
        A populated :class:`CallerIdentity`.
    """
    return CallerIdentity(gm_alias=gm_alias, properties=frozenset(properties))


def build_event(
    method: str,
    path: str,
    *,
    gm_alias: str,
    properties: list[str],
    query: dict[str, str],
) -> dict[str, Any]:
    """Build an HTTP API v2 proxy event with JWT claims and a query string.

    Args:
        method: The HTTP method.
        path: The raw request path (e.g. ``/v1/vips``).
        gm_alias: The caller's ``cognito:username`` claim.
        properties: The caller's associated properties (JSON-array claim).
        query: The query-string parameters.

    Returns:
        An API Gateway HTTP API v2 proxy event.
    """
    import json

    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "cognito:username": gm_alias,
                        "custom:properties": json.dumps(properties),
                    }
                }
            },
        },
        "queryStringParameters": dict(query),
    }
