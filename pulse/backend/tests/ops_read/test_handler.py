"""Unit tests for the VIPs/Ops facade handler (routing, scoping, errors).

Exercises :func:`handle_request` directly with an injected tool-caller seam so
the tests never open a Gateway connection: route resolution (stage-tolerant),
server-side property scoping (Requirement 16.6), a clean 5xx envelope on a
Gateway/tool failure, and the unauthenticated path via ``lambda_handler``.
"""

from __future__ import annotations

import json

from pulse.api.http import parse_request
from pulse.common.errors import OpsReadFailure
from pulse.ops_read import handler
from tests.ops_read.conftest import (
    RaisingToolCaller,
    RecordingToolCaller,
    build_event,
    make_identity,
    ok,
    unavailable,
)

_PID = "ALOHA-CHI-001"
_OTHER_PID = "ALOHA-NYC-009"


def _vips_request(path: str = "/v1/vips", property_id: str = _PID):
    """Build a normalized GET request for the VIPs route."""
    event = build_event(
        "GET",
        path,
        gm_alias="gm1",
        properties=[_PID],
        query={"propertyId": property_id},
    )
    return parse_request(event)


def _vips_caller() -> RecordingToolCaller:
    """A tool caller returning a minimal VIPs result."""
    return RecordingToolCaller(
        {"get_vip_guests": ok({"date": "2026-08-18", "vipCount": 0, "guests": []})}
    )


def test_handle_request_routes_vips_with_stage_prefix() -> None:
    """A /v1/vips path resolves to the VIPs shaping and returns 200."""
    identity = make_identity("gm1", {_PID})
    caller = _vips_caller()

    response = handler.handle_request(_vips_request(), identity, caller)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["propertyId"] == _PID
    assert "tiers" in body


def test_handle_request_rejects_property_not_in_caller_set() -> None:
    """A propertyId outside the caller's associated set is rejected 403."""
    identity = make_identity("gm1", {_PID})
    caller = _vips_caller()

    response = handler.handle_request(
        _vips_request(property_id=_OTHER_PID), identity, caller
    )

    assert response["statusCode"] == 403
    # Scoping is enforced before any tool call: the Gateway is never invoked.
    assert caller.calls == []


def test_handle_request_defaults_to_sole_property_when_hint_absent() -> None:
    """A single-property caller with no propertyId hint is scoped to their property.

    BUG-023: the PWA omitted the ?propertyId hint when its cached user lacked a
    propertyId, causing a 400 and an infinite "Loading..." state. The facade now
    derives the property from the caller's JWT claims (Requirement 16.6) when the
    caller is associated with exactly one property, so the tab works hint-less.
    """
    identity = make_identity("gm1", {_PID})
    caller = _vips_caller()
    event = build_event("GET", "/v1/vips", gm_alias="gm1", properties=[_PID], query={})

    response = handler.handle_request(parse_request(event), identity, caller)

    assert response["statusCode"] == 200
    # The tool was called scoped to the caller's sole property.
    assert caller.calls and caller.calls[0][1].get("propertyId") == _PID


def test_handle_request_requires_property_id_when_multi_property() -> None:
    """A multi-property caller with no propertyId hint yields 400 and no tool call."""
    identity = make_identity("gm1", {_PID, _OTHER_PID})
    caller = _vips_caller()
    event = build_event(
        "GET", "/v1/vips", gm_alias="gm1", properties=[_PID, _OTHER_PID], query={}
    )

    response = handler.handle_request(parse_request(event), identity, caller)

    assert response["statusCode"] == 400
    assert caller.calls == []


def test_handle_request_gateway_failure_returns_clean_5xx() -> None:
    """A Gateway/tool failure surfaces as a clean 502 envelope (no crash)."""
    identity = make_identity("gm1", {_PID})
    caller = RaisingToolCaller(
        OpsReadFailure("gateway down", reason="gateway_connect_error")
    )

    response = handler.handle_request(_vips_request(), identity, caller)

    assert response["statusCode"] == 502
    body = json.loads(response["body"])
    assert body["error"]["reason"] == "gateway_connect_error"


def test_handle_request_tool_unavailable_returns_clean_5xx() -> None:
    """A tool 'unavailable' status degrades to a clean 502 envelope."""
    identity = make_identity("gm1", {_PID})
    caller = RecordingToolCaller({"get_vip_guests": unavailable("no data")})

    response = handler.handle_request(_vips_request(), identity, caller)

    assert response["statusCode"] == 502
    body = json.loads(response["body"])
    assert body["error"]["reason"] == "gateway_tool_unavailable"


def test_handle_request_routes_ops() -> None:
    """A /ops path resolves to the Ops shaping and returns 200."""
    identity = make_identity("gm1", {_PID})
    caller = RecordingToolCaller(
        {
            "get_occupancy": ok({"date": "2026-08-18", "occupancyPct": 50}),
            "get_room_status": ok({"oooCount": 0, "rooms": []}),
            "get_work_orders": ok({"totalCount": 0, "workOrders": []}),
        }
    )
    event = build_event(
        "GET", "/v1/ops", gm_alias="gm1", properties=[_PID], query={"propertyId": _PID}
    )

    response = handler.handle_request(parse_request(event), identity, caller)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["facility"]["occupancyPct"] == 50
    assert body["oooRooms"] == []


def test_handle_request_unknown_route_is_404() -> None:
    """A path with no known facade resource yields 404."""
    identity = make_identity("gm1", {_PID})
    caller = _vips_caller()
    event = build_event(
        "GET",
        "/v1/alerts",
        gm_alias="gm1",
        properties=[_PID],
        query={"propertyId": _PID},
    )

    response = handler.handle_request(parse_request(event), identity, caller)

    assert response["statusCode"] == 404


def test_handle_request_non_get_is_405() -> None:
    """A non-GET method on a facade route yields 405."""
    identity = make_identity("gm1", {_PID})
    caller = _vips_caller()
    event = build_event(
        "POST",
        "/v1/vips",
        gm_alias="gm1",
        properties=[_PID],
        query={"propertyId": _PID},
    )

    response = handler.handle_request(parse_request(event), identity, caller)

    assert response["statusCode"] == 405


def test_lambda_handler_unauthenticated_is_401() -> None:
    """A request with no resolvable identity yields 401 before any Gateway use."""
    event = {
        "rawPath": "/v1/vips",
        "requestContext": {"http": {"method": "GET", "path": "/v1/vips"}},
        "queryStringParameters": {"propertyId": _PID},
    }

    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 401


def test_lambda_handler_missing_gateway_endpoint_is_500(monkeypatch) -> None:
    """A missing GATEWAY_ENDPOINT_URL config yields 500 (fail fast)."""
    monkeypatch.delenv("GATEWAY_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    event = build_event(
        "GET", "/v1/vips", gm_alias="gm1", properties=[_PID], query={"propertyId": _PID}
    )

    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 500
