"""Property and unit tests for the Web Push background delivery channel.

Covers Property 22 (push payloads carry the required fields within the
Requirement 13.5 length bounds) and the Requirement 13.6/13.7 retry-and-
exhaustion behavior (3 attempts at 10 s, delivery-exhausted recorded, alert
retained).
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.common.models import AlertTier
from pulse.delivery import web_push as wp

PROPERTY_SETTINGS = settings(max_examples=100)


def _subscription(endpoint: str = "https://push.example.com/ep1") -> dict[str, Any]:
    return {
        "gmAlias": "jsmith",
        "endpoint": endpoint,
        "p256dh": "key-p256dh",
        "auth": "key-auth",
    }


# ---------------------------------------------------------------------------
# Property 22: push payloads carry required fields within length bounds
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 22: Push payloads carry required
# fields within length bounds
@PROPERTY_SETTINGS
@given(
    alert_id=st.text(min_size=1, max_size=60),
    tier=st.sampled_from([t.value for t in AlertTier]),
    title=st.text(min_size=0, max_size=400),
    detail=st.text(min_size=0, max_size=2000),
)
def test_property_22_push_payload_fields_and_bounds(
    alert_id: str, tier: str, title: str, detail: str
) -> None:
    """The payload includes alertId/tier and truncates title/detail.

    Validates: Requirements 13.5
    """
    payload = wp.build_push_payload(
        alert_id=alert_id, tier=tier, title=title, detail=detail
    )
    assert payload["alertId"] == alert_id
    assert payload["tier"] == tier
    assert len(payload["title"]) <= wp.TITLE_MAX_LEN
    assert len(payload["detail"]) <= wp.DETAIL_MAX_LEN
    # Truncation preserves the leading content exactly.
    assert payload["title"] == title[: wp.TITLE_MAX_LEN]
    assert payload["detail"] == detail[: wp.DETAIL_MAX_LEN]


def test_payload_from_item_maps_alert_attributes() -> None:
    item = {
        "alertId": "alert-1",
        "tier": "CRITICAL",
        "title": "T" * 150,
        "detail": "D" * 600,
    }
    payload = wp.payload_from_item(item)
    assert payload["alertId"] == "alert-1"
    assert len(payload["title"]) == wp.TITLE_MAX_LEN
    assert len(payload["detail"]) == wp.DETAIL_MAX_LEN


def test_should_web_push_gating() -> None:
    assert wp.should_web_push(AlertTier.CRITICAL) is True
    assert wp.should_web_push(AlertTier.WARNING) is True
    # INFO is not pushed here; it is delivered by the INFO batcher.
    assert wp.should_web_push(AlertTier.INFO) is False


# ---------------------------------------------------------------------------
# Requirement 13.6 / 13.7: retry 3x at 10 s, then delivery-exhausted + retain
# ---------------------------------------------------------------------------


def test_requirement_13_6_retries_three_times_then_exhausts() -> None:
    """Delivery retries 3 times at 10 s, then records the endpoint exhausted.

    Validates: Requirements 13.6, 13.7
    """
    attempts: list[str] = []
    sleeps: list[float] = []

    def _always_fail(info: dict[str, Any], payload_json: str) -> None:
        attempts.append(info["endpoint"])
        raise RuntimeError("push service 503")

    payload = wp.build_push_payload(
        alert_id="alert-1", tier="CRITICAL", title="t", detail="d"
    )
    result = wp.deliver_web_push(
        payload, [_subscription()], sender=_always_fail, sleep=sleeps.append
    )

    assert len(attempts) == wp.WEB_PUSH_MAX_ATTEMPTS
    # Two waits between three attempts, each 10 seconds.
    assert sleeps == [10, 10]
    assert result.exhausted is True
    assert result.exhausted_endpoints == ["https://push.example.com/ep1"]
    assert result.delivered_endpoints == []


def test_requirement_13_6_succeeds_after_retry() -> None:
    """Delivery that succeeds on the second attempt records the endpoint delivered.

    Validates: Requirements 13.6
    """
    calls = {"n": 0}

    def _fail_once(info: dict[str, Any], payload_json: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    payload = wp.build_push_payload(
        alert_id="alert-1", tier="WARNING", title="t", detail="d"
    )
    result = wp.deliver_web_push(
        payload, [_subscription()], sender=_fail_once, sleep=lambda _s: None
    )

    assert calls["n"] == 2
    assert result.exhausted is False
    assert result.delivered_endpoints == ["https://push.example.com/ep1"]


def test_multiple_subscriptions_partial_exhaustion() -> None:
    """One failing endpoint is recorded exhausted while others are delivered."""

    def _sender(info: dict[str, Any], payload_json: str) -> None:
        if info["endpoint"].endswith("bad"):
            raise RuntimeError("gone")

    payload = wp.build_push_payload(
        alert_id="alert-1", tier="CRITICAL", title="t", detail="d"
    )
    subs = [
        _subscription("https://push.example.com/good"),
        _subscription("https://push.example.com/bad"),
    ]
    result = wp.deliver_web_push(
        payload, subs, sender=_sender, sleep=lambda _s: None
    )

    assert result.delivered_endpoints == ["https://push.example.com/good"]
    assert result.exhausted_endpoints == ["https://push.example.com/bad"]
