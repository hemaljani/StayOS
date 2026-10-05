"""PULSE REST API Lambda entry point (``pulse-api``): thin router.

Following PYQUALITY-05, :func:`lambda_handler` is a thin dispatcher: it extracts
the caller identity from the Cognito authorizer claims, normalizes the request,
and routes to a per-resource business-logic module. Every route is scoped
server-side to the caller's associated properties (Requirement 16.6, Property
25); no route trusts a client-supplied scope.

Routes (kebab-case plural nouns, no verbs -- NAMING-05):
    * ``GET  /alerts?propertyId=&tier=&status=``       feed + filters
    * ``GET  /alerts/{alertId}``                       detail incl. triageBrief
    * ``POST /alerts/{alertId}/acknowledgements``      acknowledge
    * ``POST /alerts/{alertId}/resolutions``           resolve
    * ``POST /alerts/{alertId}/approvals``             approve/reject (Article 14)
    * ``GET  /shift-handover?propertyId=&from=&to=``   shift-handover log
    * ``GET  /kitchen?propertyId=``                     kitchen snapshot (F&B)
    * ``GET  /rules?propertyId=``                      rule read (admin)
    * ``PUT  /rules/{ruleId}``                         rule update (admin)
    * ``POST /push-subscriptions``                     register a Web Push sub
    * ``GET  /config/vapid-public-key``                VAPID public key
    * ``GET  /config/realtime``                        AppSync Events endpoint
    * ``POST /demo/scenarios/{scenarioId}``            run a demo scenario
    * ``POST /demo/scenarios/{scenarioId}/reset``      reset a demo scenario

The demo routes drive the Demo Scenario Simulator **in-process** (the simulator
ships in this same deployment package, mirroring the in-process Action
Executor), so no cross-Lambda invoke is needed. They are exposed only when
``ENABLE_DEMO_SIMULATOR`` is truthy (default ``"true"``); a real deployment sets
it ``"false"`` and the routes return 404 as if they did not exist.

Resource names come from environment variables (PYQUALITY-06); none are
hardcoded. The router itself performs no I/O beyond delegating to the resource
modules, whose seams are individually unit-tested.
"""

from __future__ import annotations

import os
from typing import Any

from pulse.action_executor.executor import make_action_executor
from pulse.api import (
    alert_lifecycle,
    alerts_repository,
    kitchen_repository,
    rules_admin,
    subscriptions,
)
from pulse.api.http import Request, error_response, json_response, parse_request
from pulse.api.identity import CallerIdentity, extract_identity
from pulse.common.config import (
    ENV_ALERT_HISTORY_TABLE,
    ENV_ALERTS_TABLE,
    ENV_KITCHEN_TABLE,
    ENV_PUSH_SUBSCRIPTIONS_TABLE,
    ENV_RULES_TABLE,
    get_optional_env,
)
from pulse.common.errors import ConfigurationError
from pulse.common.logging import get_logger
from pulse.common.tracing import get_tracer
from pulse.delivery.realtime_publish import ENV_REALTIME_HTTP_ENDPOINT, NAMESPACE
from pulse.history.handover import query_shift_handover

logger = get_logger("pulse-api")
tracer = get_tracer("pulse-api")

# Realtime and Web Push client-config environment variables (served to the PWA
# via the /config routes; never hardcoded).
ENV_REALTIME_WSS_ENDPOINT = "REALTIME_WSS_ENDPOINT"
ENV_VAPID_PUBLIC_KEY = "VAPID_PUBLIC_KEY"

# Toggles the demo scenario routes. Truthy (default) exposes them; a real
# deployment sets it "false" so the /demo routes 404 as if absent.
ENV_ENABLE_DEMO_SIMULATOR = "ENABLE_DEMO_SIMULATOR"


def _demo_enabled() -> bool:
    """Return whether the demo scenario routes are exposed.

    Reads ``ENABLE_DEMO_SIMULATOR`` (default ``"true"``) and treats the usual
    truthy strings as enabled, so a real deployment can disable the demo surface
    with a single stack parameter.

    Returns:
        ``True`` when the demo routes should be served, else ``False``.
    """
    raw = (get_optional_env(ENV_ENABLE_DEMO_SIMULATOR, "true") or "").strip().lower()
    return raw in ("true", "1", "yes")


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


# ---------------------------------------------------------------------------
# Route handlers (each returns a proxy response envelope)
# ---------------------------------------------------------------------------


def _handle_list_alerts(request: Request, identity: CallerIdentity) -> dict[str, Any]:
    """Handle ``GET /alerts`` (feed + tier/status filters, property-scoped)."""
    alerts = alerts_repository.list_alerts(
        identity,
        requested_property=request.query.get("propertyId"),
        tier=request.query.get("tier"),
        status=request.query.get("status"),
        alerts_table_name=_required_env(ENV_ALERTS_TABLE),
    )
    return json_response(200, {"alerts": alerts, "count": len(alerts)})


def _handle_get_alert(
    request: Request, identity: CallerIdentity, alert_id: str
) -> dict[str, Any]:
    """Handle ``GET /alerts/{alertId}`` (detail incl. triage brief)."""
    item = alerts_repository.get_alert(
        identity, alert_id, alerts_table_name=_required_env(ENV_ALERTS_TABLE)
    )
    if item is None:
        return error_response(404, "Alert not found")
    return json_response(200, {"alert": item})


def _handle_acknowledge(
    request: Request, identity: CallerIdentity, alert_id: str
) -> dict[str, Any]:
    """Handle ``POST /alerts/{alertId}/acknowledgements``."""
    alerts_table = _required_env(ENV_ALERTS_TABLE)
    item = alerts_repository.get_alert(
        identity, alert_id, alerts_table_name=alerts_table
    )
    if item is None:
        return error_response(404, "Alert not found")
    result = alert_lifecycle.acknowledge_alert(
        alert_id, identity.gm_alias, item=item, alerts_table_name=alerts_table
    )
    if result.rejected:
        return error_response(
            409, "Alert cannot be acknowledged", reason=result.reason
        )
    return json_response(200, {"alertId": alert_id, "status": result.new_status.value})


def _handle_resolve(
    request: Request, identity: CallerIdentity, alert_id: str
) -> dict[str, Any]:
    """Handle ``POST /alerts/{alertId}/resolutions``."""
    alerts_table = _required_env(ENV_ALERTS_TABLE)
    item = alerts_repository.get_alert(
        identity, alert_id, alerts_table_name=alerts_table
    )
    if item is None:
        return error_response(404, "Alert not found")
    result = alert_lifecycle.resolve_alert(
        alert_id, identity.gm_alias, item=item, alerts_table_name=alerts_table
    )
    if result.rejected:
        return error_response(409, "Alert cannot be resolved", reason=result.reason)
    return json_response(200, {"alertId": alert_id, "status": result.new_status.value})


def _handle_approval(
    request: Request, identity: CallerIdentity, alert_id: str
) -> dict[str, Any]:
    """Handle ``POST /alerts/{alertId}/approvals`` (Article 14 gate)."""
    alerts_table = _required_env(ENV_ALERTS_TABLE)
    item = alerts_repository.get_alert(
        identity, alert_id, alerts_table_name=alerts_table
    )
    if item is None:
        return error_response(404, "Alert not found")
    decision = str(request.body.get("decision", ""))
    selected_option = request.body.get("selectedOption")
    # In-process executor: on an accepted approval the real Action Executor
    # (Task 18) runs the write-back + transactional RESOLVE inside this Lambda
    # (same deployment package). See module docstring for the invocation choice.
    result = alert_lifecycle.decide_approval(
        alert_id,
        identity.gm_alias,
        decision,
        selected_option if selected_option is None else str(selected_option),
        item=item,
        alerts_table_name=alerts_table,
        action_executor=make_action_executor(alerts_table),
    )
    if not result.get("accepted"):
        return error_response(
            409, "Approval decision rejected", reason=result.get("reason")
        )
    return json_response(200, result)


def _handle_shift_handover(
    request: Request, identity: CallerIdentity
) -> dict[str, Any]:
    """Handle ``GET /shift-handover?propertyId=&from=&to=`` (property-scoped)."""
    property_id = request.query.get("propertyId")
    start = request.query.get("from")
    end = request.query.get("to")
    if not property_id or not start or not end:
        return error_response(400, "propertyId, from, and to are required")
    if not identity.is_associated_with(property_id):
        # Out of scope: return an empty window rather than leak existence.
        return json_response(200, {"alerts": [], "count": 0})
    alerts = query_shift_handover(
        property_id,
        start,
        end,
        history_table_name=_required_env(ENV_ALERT_HISTORY_TABLE),
    )
    return json_response(200, {"alerts": alerts, "count": len(alerts)})


def _handle_kitchen(request: Request, identity: CallerIdentity) -> dict[str, Any]:
    """Handle ``GET /kitchen?propertyId=`` (kitchen snapshot, property-scoped).

    Requires a ``propertyId`` query parameter and enforces the caller's
    server-side property scope before reading the snapshot (Requirement 16.6,
    Property 25). Returns 400 when ``propertyId`` is missing, 403 when the caller
    is not associated with it, and 404 when no snapshot exists for the property.

    Args:
        request: The normalized request.
        identity: The authenticated caller.

    Returns:
        The proxy response envelope with the kitchen snapshot, or an error.
    """
    property_id = request.query.get("propertyId")
    if not property_id:
        return error_response(400, "propertyId is required")
    if not identity.is_associated_with(property_id):
        return error_response(403, "Not authorized for this property")
    item = kitchen_repository.get_kitchen(
        identity, property_id, kitchen_table_name=_required_env(ENV_KITCHEN_TABLE)
    )
    if item is None:
        return error_response(404, "Kitchen snapshot not found")
    return json_response(
        200,
        {
            "propertyId": property_id,
            "banquetCountdown": item.get("banquetCountdown"),
            "fbStats": item.get("fbStats", []),
            "deliverySla": item.get("deliverySla"),
            "kitchenOrders": item.get("kitchenOrders", []),
            "channelMix": item.get("channelMix", []),
            "channelMixNote": item.get("channelMixNote", ""),
        },
    )


def _handle_list_rules(request: Request, identity: CallerIdentity) -> dict[str, Any]:
    """Handle ``GET /rules?propertyId=`` (property-scoped)."""
    rules = rules_admin.list_rules(
        identity,
        requested_property=request.query.get("propertyId"),
        rules_table_name=_required_env(ENV_RULES_TABLE),
    )
    return json_response(200, {"rules": rules, "count": len(rules)})


def _handle_update_rule(
    request: Request, identity: CallerIdentity, rule_id: str
) -> dict[str, Any]:
    """Handle ``PUT /rules/{ruleId}`` (validate; retain prior on reject)."""
    result = rules_admin.update_rule(
        rule_id,
        request.body,
        identity,
        rules_table_name=_required_env(ENV_RULES_TABLE),
    )
    if result.get("denied"):
        return error_response(403, "Not authorized for this property")
    if not result.get("accepted"):
        return error_response(
            400,
            "Rule update rejected; prior definition retained",
            invalidAttribute=result.get("invalidAttribute"),
        )
    return json_response(200, result)


def _handle_register_subscription(
    request: Request, identity: CallerIdentity
) -> dict[str, Any]:
    """Handle ``POST /push-subscriptions``."""
    try:
        result = subscriptions.register_subscription(
            request.body,
            identity,
            subscriptions_table_name=_required_env(ENV_PUSH_SUBSCRIPTIONS_TABLE),
        )
    except subscriptions.InvalidSubscriptionError as exc:
        return error_response(400, "Invalid subscription", missing=exc.missing)
    return json_response(201, result)


def _handle_vapid_public_key(
    request: Request, identity: CallerIdentity
) -> dict[str, Any]:
    """Handle ``GET /config/vapid-public-key``."""
    public_key = get_optional_env(ENV_VAPID_PUBLIC_KEY, "")
    return json_response(200, {"publicKey": public_key})


def _handle_realtime_config(
    request: Request, identity: CallerIdentity
) -> dict[str, Any]:
    """Handle ``GET /config/realtime`` (AppSync Events endpoint + namespace)."""
    return json_response(
        200,
        {
            "httpEndpoint": get_optional_env(ENV_REALTIME_HTTP_ENDPOINT, ""),
            "wssEndpoint": get_optional_env(ENV_REALTIME_WSS_ENDPOINT, ""),
            "namespace": NAMESPACE,
        },
    )


def _handle_demo_scenario(
    request: Request, identity: CallerIdentity, scenario_id: str, action: str
) -> dict[str, Any]:
    """Handle a demo scenario ``run``/``reset`` by invoking the simulator.

    Drives the Demo Scenario Simulator in-process (it lives in this same
    deployment package): resolves the scenario from the declarative catalog and
    applies the deterministic mutation to the operational table. An unknown
    scenario id yields 404 and an invalid action yields 400.

    Args:
        request: The normalized request (unused; present for signature parity).
        identity: The authenticated caller; the demo targets the caller's OWN
            property so each GM's "Generate Alerts" populates their own hotel's
            feed (falls back to the canonical demo property when the caller has
            no single associated property).
        scenario_id: The scenario identifier from the path.
        action: ``"run"`` (raise the condition) or ``"reset"`` (clear it).

    Returns:
        The proxy response envelope with the applied mutation summary.
    """
    # Imported lazily so the demo simulator (and its operational-schema seams)
    # is only loaded on the demo path, never for the core alert API.
    from pulse.demo_simulator import simulator

    # Target the caller's OWN property so the generated alerts land in the feed
    # the logged-in GM actually sees (property-scoped). A single-property GM (the
    # StayOS norm) resolves to their one property; otherwise fall back to the
    # canonical demo property so the button still works for a multi/none case.
    if len(identity.properties) == 1:
        property_id = next(iter(identity.properties))
    else:
        property_id = simulator.DEMO_PROPERTY_ID

    try:
        scenario = simulator.get_scenario(scenario_id)
    except simulator.ScenarioError:
        return error_response(404, "Unknown demo scenario", scenarioId=scenario_id)
    try:
        # For a run, mint a per-invocation variant so each button press raises a
        # NEW, distinct condition (fresh dedupeKey -> fresh alert) that stacks on
        # top of existing alerts instead of deduping onto them. Reset clears the
        # base entity (variant is ignored by the simulator for reset).
        variant = simulator.mint_variant() if action == "run" else None
        plan = simulator.apply_scenario(
            scenario, action, variant=variant, property_id=property_id
        )
    except simulator.ScenarioError as exc:
        return error_response(400, "Invalid demo scenario action", detail=str(exc))
    return json_response(
        200,
        {
            "scenarioId": scenario.scenario_id,
            "action": plan.action,
            "operation": plan.operation,
            "tableKind": plan.table_kind,
            "propertyId": property_id,
            "expectedAlertType": scenario.expected_alert_type.value,
        },
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route(request: Request, identity: CallerIdentity) -> dict[str, Any]:
    """Route a parsed request to its handler (pure dispatch).

    Args:
        request: The normalized request.
        identity: The authenticated caller.

    Returns:
        The proxy response envelope for the matched route, or a 404/405 when no
        route matches.
    """
    segments = request.segments
    method = request.method
    top = request.segment(0)

    if top == "alerts":
        return _route_alerts(request, identity, method, segments)
    if top == "shift-handover" and method == "GET":
        return _handle_shift_handover(request, identity)
    if top == "kitchen" and method == "GET":
        return _handle_kitchen(request, identity)
    if top == "rules":
        return _route_rules(request, identity, method, segments)
    if top == "push-subscriptions" and method == "POST" and len(segments) == 1:
        return _handle_register_subscription(request, identity)
    if top == "config" and method == "GET" and len(segments) == 2:
        if segments[1] == "vapid-public-key":
            return _handle_vapid_public_key(request, identity)
        if segments[1] == "realtime":
            return _handle_realtime_config(request, identity)
    if top == "demo" and _demo_enabled():
        return _route_demo(request, identity, method, segments)
    return error_response(404, "No matching route")


def _route_alerts(
    request: Request,
    identity: CallerIdentity,
    method: str,
    segments: list[str],
) -> dict[str, Any]:
    """Route ``/alerts`` and its sub-resources.

    Args:
        request: The normalized request.
        identity: The authenticated caller.
        method: The HTTP method.
        segments: The path segments (``segments[0] == "alerts"``).

    Returns:
        The proxy response envelope.
    """
    if len(segments) == 1:
        if method == "GET":
            return _handle_list_alerts(request, identity)
        return error_response(405, "Method not allowed")
    alert_id = segments[1]
    if len(segments) == 2 and method == "GET":
        return _handle_get_alert(request, identity, alert_id)
    if len(segments) == 3 and method == "POST":
        sub_resource = segments[2]
        if sub_resource == "acknowledgements":
            return _handle_acknowledge(request, identity, alert_id)
        if sub_resource == "resolutions":
            return _handle_resolve(request, identity, alert_id)
        if sub_resource == "approvals":
            return _handle_approval(request, identity, alert_id)
    return error_response(404, "No matching route")


def _route_rules(
    request: Request,
    identity: CallerIdentity,
    method: str,
    segments: list[str],
) -> dict[str, Any]:
    """Route ``/rules`` and its sub-resources.

    Args:
        request: The normalized request.
        identity: The authenticated caller.
        method: The HTTP method.
        segments: The path segments (``segments[0] == "rules"``).

    Returns:
        The proxy response envelope.
    """
    if len(segments) == 1 and method == "GET":
        return _handle_list_rules(request, identity)
    if len(segments) == 2 and method == "PUT":
        return _handle_update_rule(request, identity, segments[1])
    return error_response(404, "No matching route")


def _route_demo(
    request: Request,
    identity: CallerIdentity,
    method: str,
    segments: list[str],
) -> dict[str, Any]:
    """Route ``/demo/scenarios/{scenarioId}`` and its ``/reset`` sub-resource.

    Only reached when the demo surface is enabled (the caller in :func:`route`
    checks :func:`_demo_enabled`), so a disabled deployment never dispatches
    here and the path 404s from the top-level router.

    Args:
        request: The normalized request.
        identity: The authenticated caller.
        method: The HTTP method.
        segments: The path segments (``segments[0] == "demo"``).

    Returns:
        The proxy response envelope.
    """
    # /demo/scenarios/{scenarioId}  -> run
    if len(segments) == 3 and segments[1] == "scenarios" and method == "POST":
        return _handle_demo_scenario(request, identity, segments[2], action="run")
    # /demo/scenarios/{scenarioId}/reset -> reset
    if (
        len(segments) == 4
        and segments[1] == "scenarios"
        and segments[3] == "reset"
        and method == "POST"
    ):
        return _handle_demo_scenario(request, identity, segments[2], action="reset")
    return error_response(404, "No matching route")


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """PULSE REST API Lambda handler (thin dispatcher).

    Extracts the caller identity, normalizes the request, and routes it. Domain
    errors surface as a 400 and unexpected failures as a 500; both are logged
    with request context. The router never executes an action without the
    server-side property scope derived from the caller's claims.

    Args:
        event: The API Gateway proxy invocation event (HTTP API v2 or REST).
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        An API Gateway proxy response envelope.
    """
    request = parse_request(event)
    # CORS preflight: the browser sends an unauthenticated OPTIONS before the
    # real cross-origin request. It is routed here via the unauthenticated
    # "OPTIONS /{proxy+}" route, so short-circuit with 204 BEFORE the identity
    # check (a preflight carries no Authorization header). API Gateway's
    # CorsConfiguration appends the Access-Control-* headers to this response;
    # returning 401 here would make the browser block the real request
    # ("Failed to fetch").
    if request.method == "OPTIONS":
        return json_response(204, {})

    identity = extract_identity(event)
    if not identity.gm_alias:
        # A valid Cognito authorizer always yields an identity; its absence is
        # an unauthenticated/misconfigured request (Requirement 16.2).
        logger.warning(
            "Request without a resolvable identity", extra={"path": request.raw_path}
        )
        return error_response(401, "Unauthorized")
    try:
        return route(request, identity)
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


__all__ = [
    "ENV_REALTIME_WSS_ENDPOINT",
    "ENV_VAPID_PUBLIC_KEY",
    "ENV_ENABLE_DEMO_SIMULATOR",
    "route",
    "lambda_handler",
]
