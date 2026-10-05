"""Unit tests for the dual-channel delivery orchestration (pulse-push-service).

Verifies that every alert publishes a realtime event and that only
CRITICAL/WARNING alerts additionally trigger a background Web Push, and that the
escalation ``DeliverFn`` factory nudges the current recipient over both channels.
"""

from __future__ import annotations

from typing import Any

from pulse.delivery import push_service as ps
from pulse.delivery import realtime_publish as rt


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def __call__(self, channel: str, events: Any) -> None:
        self.calls.append((channel, list(events)))


def _item(tier: str = "CRITICAL", alert_id: str = "alert-1") -> dict[str, Any]:
    return {
        "alertId": alert_id,
        "propertyId": "ALOHA-CHI-001",
        "gmAlias": "jsmith",
        "tier": tier,
        "type": "WALK_RISK",
        "status": "UNACKNOWLEDGED",
        "title": "Walk Risk",
        "detail": "374 confirmed vs 368 available",
        "lastStatusChangeAt": "2026-08-17T14:30:00Z",
    }


def test_critical_alert_publishes_realtime_and_web_push() -> None:
    publisher = _RecordingPublisher()
    sent: list[str] = []

    def _sender(info: dict[str, Any], payload_json: str) -> None:
        sent.append(info["endpoint"])

    summary = ps.deliver_alert(
        _item("CRITICAL"),
        rt.EVENT_ALERT_CREATED,
        subscription_loader=lambda alias: [
            {"endpoint": "https://push/ep", "p256dh": "k", "auth": "a"}
        ],
        web_push_sender=_sender,
        realtime_publisher=publisher,
        sleep=lambda _s: None,
    )

    assert publisher.calls[0][0] == "/pulse/alerts/ALOHA-CHI-001"
    assert summary["webPushAttempted"] is True
    assert summary["webPushDelivered"] == 1
    assert sent == ["https://push/ep"]


def test_info_alert_publishes_realtime_but_skips_web_push() -> None:
    publisher = _RecordingPublisher()
    called = {"subs": 0}

    def _loader(alias: str) -> list[dict[str, Any]]:
        called["subs"] += 1
        return []

    summary = ps.deliver_alert(
        _item("INFO"),
        rt.EVENT_ALERT_CREATED,
        subscription_loader=_loader,
        web_push_sender=lambda info, data: None,
        realtime_publisher=publisher,
    )

    assert summary["realtimePublished"] is True
    assert summary.get("webPushSkipped") is True
    # INFO never touches the subscription loader or Web Push sender.
    assert called["subs"] == 0
    assert len(publisher.calls) == 1


def test_make_escalation_deliver_nudges_recipient_over_both_channels() -> None:
    publisher = _RecordingPublisher()
    sent: list[str] = []

    deliver = ps.make_escalation_deliver(
        alert_loader=lambda alert_id: _item("CRITICAL", alert_id),
        subscription_loader=lambda alias: [
            {"endpoint": f"https://push/{alias}", "p256dh": "k", "auth": "a"}
        ],
        web_push_sender=lambda info, data: sent.append(info["endpoint"]),
        realtime_publisher=publisher,
        sleep=lambda _s: None,
    )

    deliver("alert-1", "agm")

    channels = [channel for channel, _batch in publisher.calls]
    # Broadcast feed update plus a unicast nudge to the current recipient.
    assert "/pulse/alerts/ALOHA-CHI-001" in channels
    assert "/pulse/alerts/ALOHA-CHI-001/agm" in channels
    # The Web Push goes to the recipient's subscription.
    assert sent == ["https://push/agm"]


def test_make_escalation_deliver_raises_on_unknown_alert() -> None:
    deliver = ps.make_escalation_deliver(
        alert_loader=lambda alert_id: None,
        subscription_loader=lambda alias: [],
        web_push_sender=lambda info, data: None,
    )
    try:
        deliver("missing", "agm")
    except ValueError:
        pass
    else:  # pragma: no cover - explicit failure if no raise
        raise AssertionError("expected ValueError for unknown alert")


def test_deliver_alert_invokes_latency_recorder_with_tier() -> None:
    recorded: list[tuple[str, str]] = []

    def _recorder(item: dict[str, Any], tier_value: str) -> None:
        recorded.append((item["alertId"], tier_value))

    ps.deliver_alert(
        _item("INFO", "alert-info"),
        rt.EVENT_ALERT_CREATED,
        subscription_loader=lambda alias: [],
        web_push_sender=lambda info, data: None,
        realtime_publisher=_RecordingPublisher(),
        latency_recorder=_recorder,
    )

    assert recorded == [("alert-info", "INFO")]


def test_deliver_alert_survives_latency_recorder_failure() -> None:
    """A raising latency recorder must not interrupt delivery (Req 17.4)."""

    def _raising_recorder(item: dict[str, Any], tier_value: str) -> None:
        raise RuntimeError("emitter blew up")

    summary = ps.deliver_alert(
        _item("CRITICAL", "alert-x"),
        rt.EVENT_ALERT_CREATED,
        subscription_loader=lambda alias: [
            {"endpoint": "https://push/ep", "p256dh": "k", "auth": "a"}
        ],
        web_push_sender=lambda info, data: None,
        realtime_publisher=_RecordingPublisher(),
        latency_recorder=_raising_recorder,
        sleep=lambda _s: None,
    )

    # Delivery still completed and reported success despite the recorder failure.
    assert summary["realtimePublished"] is True
    assert summary["webPushAttempted"] is True
