"""C3b VIPs/Ops facade Lambda entry point (``pulse-ops-read``): thin router.

Following PYQUALITY-05, :func:`lambda_handler` is a thin dispatcher: it extracts
the caller identity from the Cognito authorizer claims, normalizes the request,
validates the requested ``propertyId`` against the caller's associated set
(server-side property scoping, Requirement 16.6), then opens a lazy MCP
connection to the shared StayOS AgentCore Gateway and delegates to the per-tab
shaping functions.

Routes (kebab-case plural nouns, no verbs -- NAMING-05):
    * ``GET /vips?propertyId=`` -> VIP arrivals grouped by tier (``vips.shape_vips``)
    * ``GET /ops?propertyId=``  -> facility summary + OOO cards + group checkout
      (``ops.shape_ops``)

The routing/scoping/shaping is factored into :func:`handle_request`, which takes
the Gateway tool-call seam as a parameter so it is fully unit-testable with an
in-memory fake (``mcp_proxy_for_aws`` / ``strands`` are never required to run the
tests). :func:`lambda_handler` builds a *lazy* real caller so a 400/403/404
short-circuit never opens a Gateway connection.

Resource identifiers come from environment variables (PYQUALITY-06): the Gateway
endpoint from ``GATEWAY_ENDPOINT_URL`` (SSM ``/${StackPrefix}/gateway/endpoint-url``
at deploy) and the region from ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` (the Lambda
runtime default); none are hardcoded.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from pulse.api.http import Request, error_response, json_response, parse_request
from pulse.api.identity import CallerIdentity, extract_identity
from pulse.common.errors import ConfigurationError, OpsReadFailure
from pulse.common.logging import get_logger
from pulse.common.tracing import get_tracer
from pulse.ops_read import ops, vips
from pulse.ops_read.gateway import GATEWAY_AWS_SERVICE, GatewayToolClient, ToolCaller

logger = get_logger("pulse-ops-read")
tracer = get_tracer("pulse-ops-read")

# The shared Gateway MCP endpoint URL (SSM /${StackPrefix}/gateway/endpoint-url,
# injected as this env var at deploy). Never hardcoded (PYQUALITY-06).
ENV_GATEWAY_ENDPOINT_URL = "GATEWAY_ENDPOINT_URL"

# The resources this facade serves; used to resolve the route from the path in a
# way that tolerates a leading stage segment (e.g. "/v1/vips").
_VIPS_RESOURCE = "vips"
_OPS_RESOURCE = "ops"
_KNOWN_RESOURCES = (_VIPS_RESOURCE, _OPS_RESOURCE)


def _required_env(name: str) -> str:
    """Read a required environment variable or fail fast.

    Args:
        name: The environment variable name.

    Returns:
        The non-empty value.

    Raises:
        ConfigurationError: When the variable is unset or empty.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"Required environment variable {name!r} is not set", variable=name
        )
    return value


def _resolve_region() -> str:
    """Resolve the AWS region from the Lambda runtime environment.

    Prefers ``AWS_REGION`` (always set in Lambda) and falls back to
    ``AWS_DEFAULT_REGION``. Never hardcoded (PYQUALITY-06).

    Returns:
        The region string.

    Raises:
        ConfigurationError: When neither region variable is set.
    """
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()
    if not region:
        raise ConfigurationError(
            "No AWS region resolved from AWS_REGION/AWS_DEFAULT_REGION",
            variable="AWS_REGION",
        )
    return region


def _resolve_resource(request: Request) -> Optional[str]:
    """Resolve which facade resource (``vips``/``ops``) the request targets.

    Tolerates a leading stage segment (e.g. ``/v1/vips``) by scanning the path
    segments for a known resource, so routing works regardless of the deployed
    stage.

    Args:
        request: The normalized request.

    Returns:
        ``"vips"`` or ``"ops"``, or ``None`` when no known resource is present.
    """
    for segment in request.segments:
        if segment in _KNOWN_RESOURCES:
            return segment
    return None


def handle_request(
    request: Request, identity: CallerIdentity, tool_caller: ToolCaller
) -> dict[str, Any]:
    """Route, property-scope, and shape a VIPs/Ops facade request (testable).

    Enforces server-side property scoping before any tool call: the requested
    ``propertyId`` must be in the caller's associated set, else the request is
    rejected 403 and ``tool_caller`` is never invoked (Requirement 16.6). A
    Gateway/tool failure surfaces as a clean 502 error envelope (no crash).

    Args:
        request: The normalized request.
        identity: The authenticated caller.
        tool_caller: The Gateway tool-call seam (real or an injected fake).

    Returns:
        The API Gateway proxy response envelope.
    """
    if request.method != "GET":
        return error_response(405, "Method not allowed")

    resource = _resolve_resource(request)
    if resource is None:
        return error_response(404, "No matching route")

    # Resolve the target property. The client MAY pass a ?propertyId= hint, but
    # scoping is authoritative from the caller's JWT claims (Requirement 16.6,
    # BUG-013): when no hint is given, default to the caller's own property so
    # the tab works without relying on a client-populated propertyId (BUG-023 -
    # the PWA omitted the hint when its cached user had no propertyId, causing a
    # 400 and an infinite "Loading..." state). A single-property GM (the StayOS
    # norm) needs no hint; a multi-property operator must name which one.
    property_id = request.query.get("propertyId")
    if not property_id:
        if len(identity.properties) == 1:
            property_id = next(iter(identity.properties))
        else:
            return error_response(400, "propertyId is required")

    # Server-side property scoping: never trust a client-supplied propertyId
    # beyond validating it is in the caller's associated set.
    if not identity.is_associated_with(property_id):
        return error_response(403, "Not authorized for this property")

    try:
        if resource == _VIPS_RESOURCE:
            body = vips.shape_vips(property_id, tool_caller)
        else:
            body = ops.shape_ops(property_id, tool_caller)
    except OpsReadFailure as exc:
        # Gateway/tool degradation -> clean 5xx envelope, never a crash.
        logger.error(
            "Ops-read facade could not read Gateway data",
            extra={
                "resource": resource,
                "property_id": property_id,
                "tool": exc.tool,
                "reason": exc.reason,
            },
        )
        return error_response(
            502, "Live operations data is temporarily unavailable", reason=exc.reason
        )
    return json_response(200, body)


def _make_lazy_tool_caller(client: GatewayToolClient) -> ToolCaller:
    """Build a tool caller that opens the Gateway connection on first use.

    Deferring the connect keeps a 400/403/404 short-circuit from ever opening a
    network connection (only a request that passes scoping and reaches shaping
    pays the connect cost).

    Args:
        client: The (unconnected) Gateway client to drive.

    Returns:
        A :data:`ToolCaller` that connects lazily then delegates to
        :meth:`GatewayToolClient.call_tool`.
    """
    state = {"connected": False}

    def _call(tool_name: str, arguments: dict[str, Any]) -> Any:
        if not state["connected"]:
            client.connect()
            state["connected"] = True
        return client.call_tool(tool_name, arguments)

    return _call


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """PULSE VIPs/Ops facade Lambda handler (thin dispatcher).

    Extracts the caller identity, normalizes the request, and delegates to
    :func:`handle_request` with a lazy Gateway tool caller. Configuration errors
    surface as 500 and unexpected failures as 500; both are logged with request
    context. The Gateway connection is always closed on the way out.

    Args:
        event: The API Gateway proxy invocation event (HTTP API v2 or REST).
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        An API Gateway proxy response envelope.
    """
    request = parse_request(event)
    identity = extract_identity(event)
    if not identity.gm_alias:
        # A valid Cognito authorizer always yields an identity; its absence is
        # an unauthenticated/misconfigured request (Requirement 16.2).
        logger.warning(
            "Request without a resolvable identity", extra={"path": request.raw_path}
        )
        return error_response(401, "Unauthorized")

    client: Optional[GatewayToolClient] = None
    try:
        endpoint = _required_env(ENV_GATEWAY_ENDPOINT_URL)
        region = _resolve_region()
        client = GatewayToolClient(endpoint, region, GATEWAY_AWS_SERVICE)
        return handle_request(request, identity, _make_lazy_tool_caller(client))
    except ConfigurationError as exc:
        logger.error(
            "Configuration error handling request",
            extra={"path": request.raw_path, "variable": exc.variable},
        )
        return error_response(500, "Server configuration error")
    except Exception as exc:  # noqa: BLE001 - top-level API boundary handler
        logger.error(
            "Unhandled error processing request",
            extra={
                "method": request.method,
                "path": request.raw_path,
                "error": str(exc),
            },
        )
        return error_response(500, "Internal server error")
    finally:
        if client is not None:
            client.close()


__all__ = ["ENV_GATEWAY_ENDPOINT_URL", "handle_request", "lambda_handler"]
