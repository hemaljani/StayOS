"""Tests for the AppSync Events realtime publish helper.

Covers channel-path construction, the <=5-events-per-publish batching contract
(no event dropped or duplicated), best-effort non-blocking publish semantics,
and the ``realtime_publish`` convenience that fans out to the broadcast channel
plus an optional per-user unicast channel.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.delivery import realtime_publish as rt

PROPERTY_SETTINGS = settings(max_examples=100)


class _RecordingPublisher:
    """A publisher seam that records (channel, batch) calls."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.fail = fail

    def __call__(self, channel: str, events: Any) -> None:
        self.calls.append((channel, list(events)))
        if self.fail:
            raise RuntimeError("publish endpoint unavailable")


def _item(alert_id: str = "alert-1") -> dict[str, Any]:
    return {
        "alertId": alert_id,
        "propertyId": "ALOHA-CHI-001",
        "tier": "CRITICAL",
        "type": "WALK_RISK",
        "status": "UNACKNOWLEDGED",
        "title": "Walk Risk +6",
        "escalationStatus": "MANDATORY_GM_REVIEW",
        "triageBrief": {"summary": "x"},
        "lastStatusChangeAt": "2026-08-17T14:30:00Z",
    }


def test_channel_builders() -> None:
    assert rt.broadcast_channel("P1") == "/pulse/alerts/P1"
    assert rt.unicast_channel("P1", "jsmith") == "/pulse/alerts/P1/jsmith"


def test_event_from_item_omits_heavy_brief_but_flags_presence() -> None:
    event = rt.event_from_item(_item(), rt.EVENT_ALERT_CREATED)
    assert event["eventType"] == "ALERT_CREATED"
    assert event["alertId"] == "alert-1"
    assert event["type"] == "WALK_RISK"
    assert event["hasTriageBrief"] is True
    # The heavy triageBrief payload is never inlined into the event.
    assert "triageBrief" not in event


@PROPERTY_SETTINGS
@given(count=st.integers(min_value=0, max_value=53))
def test_chunk_events_batches_of_at_most_five_no_loss(count: int) -> None:
    events = [{"alertId": f"a{i}"} for i in range(count)]
    batches = rt.chunk_events(events)
    # Every batch is within the AppSync Events 5-event publish limit.
    assert all(len(batch) <= rt.MAX_EVENTS_PER_PUBLISH for batch in batches)
    # The union of batches equals the input exactly, in order (no loss/dup).
    flattened = [event for batch in batches for event in batch]
    assert flattened == events


def test_publish_chunks_and_reports_success() -> None:
    publisher = _RecordingPublisher()
    events = [{"alertId": f"a{i}"} for i in range(12)]
    ok = rt.publish("/pulse/alerts/P1", events, publisher=publisher)
    assert ok is True
    # 12 events -> batches of 5, 5, 2.
    assert [len(batch) for _channel, batch in publisher.calls] == [5, 5, 2]


def test_publish_is_best_effort_and_never_raises() -> None:
    publisher = _RecordingPublisher(fail=True)
    ok = rt.publish("/pulse/alerts/P1", [{"alertId": "a"}], publisher=publisher)
    # A publisher failure is swallowed and reported as False, never raised.
    assert ok is False


def test_publish_skips_when_no_publisher_configured(
    monkeypatch: Any,
) -> None:
    # With no REALTIME_HTTP_ENDPOINT set, the default publisher resolves to None
    # and publishing is skipped (best-effort), not an error.
    monkeypatch.delenv(rt.ENV_REALTIME_HTTP_ENDPOINT, raising=False)
    ok = rt.publish("/pulse/alerts/P1", [{"alertId": "a"}])
    assert ok is False


def test_realtime_publish_broadcast_and_unicast() -> None:
    publisher = _RecordingPublisher()
    rt.realtime_publish(
        rt.EVENT_ALERT_UPDATED,
        _item(),
        unicast_gm_alias="agm",
        publisher=publisher,
    )
    channels = [channel for channel, _batch in publisher.calls]
    assert channels == [
        "/pulse/alerts/ALOHA-CHI-001",
        "/pulse/alerts/ALOHA-CHI-001/agm",
    ]


def test_realtime_publish_broadcast_only_without_unicast() -> None:
    publisher = _RecordingPublisher()
    rt.realtime_publish(rt.EVENT_ALERT_RESOLVED, _item(), publisher=publisher)
    channels = [channel for channel, _batch in publisher.calls]
    assert channels == ["/pulse/alerts/ALOHA-CHI-001"]
