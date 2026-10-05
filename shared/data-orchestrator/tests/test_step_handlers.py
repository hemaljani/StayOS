"""Unit tests for the six thin orchestrator step Lambdas.

Each handler must parse the step contract, delegate to its unit-testable
business function, and return a serialized step-result envelope carrying
``propertyId`` / ``referenceDate`` context (Requirements 9.1, 9.2). These are
Task 2 scaffold stubs, so the assertions check the contract and structured
shape rather than real generation output.
"""

from __future__ import annotations

import json

import pytest

import generate_handler
import prime_baseline_handler
import quiesce_handler
import reconcile_handler
import regenerate_brief_handler
import unquiesce_handler
from orchestrator_common import MODE_ROLL_FORWARD, MODE_SEED, STATUS_OK

from conftest import make_lambda_context

ROLL_FORWARD_EVENT = {
    "mode": MODE_ROLL_FORWARD,
    "propertyId": "ALOHA-CHI-001",
    "referenceDate": "2026-08-17",
}

# Full pilot estate, so seed-mode fan-out expectations stay in sync with the
# generator config the Generate step sources its pilot list from.
PILOT = ["ALOHA-CHI-001", "ALOHA-MIA-001", "ALOHA-TYO-001", "ALOHA-MAD-001", "ALOHA-BOM-001"]

CTX = make_lambda_context()


# Per-property GM settings as stored in stayos-settings (gmAlias PK), mirroring
# the shape schedule_manager reads: propertyId, timezone (IANA), briefDeliveryTime.
_SETTINGS_BY_PROPERTY = {
    "ALOHA-CHI-001": {"gmAlias": "jsmith", "timezone": "America/Chicago", "briefDeliveryTime": "06:30"},
    "ALOHA-MIA-001": {"gmAlias": "mrodriguez", "timezone": "America/New_York", "briefDeliveryTime": "06:30"},
    "ALOHA-TYO-001": {"gmAlias": "ttanaka", "timezone": "Asia/Tokyo", "briefDeliveryTime": "06:30"},
    "ALOHA-MAD-001": {"gmAlias": "cgarcia", "timezone": "Europe/Madrid", "briefDeliveryTime": "06:30"},
    "ALOHA-BOM-001": {"gmAlias": "pdesai", "timezone": "Asia/Kolkata", "briefDeliveryTime": "06:30"},
}


class _FakeLambdaClient:
    """Minimal fake Lambda client recording generate-single invocations."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        body = json.dumps({"statusCode": 200, "propertyId": "ok"}).encode("utf-8")
        return {"Payload": _FakePayload(body)}


class _FakePayload:
    """Stand-in for the streaming body returned by Lambda invoke."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture()
def mock_brief_regen(monkeypatch: pytest.MonkeyPatch):
    """Mock the boto3 Lambda client and settings read for the RegenerateBrief step.

    Returns the fake Lambda client so tests can assert on the recorded
    generate-single invocations without hitting AWS.
    """
    fake_client = _FakeLambdaClient()
    monkeypatch.setenv("LUMI_ORCHESTRATOR_FUNCTION_NAME", "stayos-orchestrator")
    monkeypatch.setenv("SETTINGS_TABLE_NAME", "stayos-settings-test")
    monkeypatch.setattr(regenerate_brief_handler, "_get_lambda_client", lambda: fake_client)
    monkeypatch.setattr(
        regenerate_brief_handler,
        "_read_property_schedule",
        lambda property_id: (
            regenerate_brief_handler.PropertySchedule(
                gm_alias=_SETTINGS_BY_PROPERTY[property_id]["gmAlias"],
                property_id=property_id,
                delivery_time=_SETTINGS_BY_PROPERTY[property_id]["briefDeliveryTime"],
                timezone=_SETTINGS_BY_PROPERTY[property_id]["timezone"],
            )
            if property_id in _SETTINGS_BY_PROPERTY
            else None
        ),
    )
    return fake_client


def _assert_envelope(result: dict, expected_step: str) -> None:
    """Assert the common step-result envelope shape and context."""
    assert result["step"] == expected_step
    assert result["status"] == STATUS_OK
    assert result["propertyId"] == "ALOHA-CHI-001"
    assert result["referenceDate"] == "2026-08-17"
    assert isinstance(result["details"], dict)
    assert result["summary"]


class TestQuiesceHandler:
    def test_returns_quiesce_envelope(self) -> None:
        result = quiesce_handler.lambda_handler(dict(ROLL_FORWARD_EVENT), CTX)
        _assert_envelope(result, "Quiesce")
        assert result["details"]["quiesced"] is True

    def test_invalid_input_raises(self) -> None:
        with pytest.raises(Exception):
            quiesce_handler.lambda_handler({"mode": "bogus"}, CTX)


class TestGenerateHandler:
    def test_returns_generate_envelope(self, mock_dataset_stack) -> None:
        # Real Task 3 wiring: run against moto-backed tables. Reload the handler
        # module so it uses the moto-bound clients from the fixture.
        import generate_handler as gen

        result = gen.lambda_handler(dict(ROLL_FORWARD_EVENT), CTX)
        _assert_envelope(result, "Generate")
        # Roll-forward scopes to one property.
        assert result["details"]["targetProperties"] == ["ALOHA-CHI-001"]
        assert result["details"]["upsertMode"] == "idempotent"
        # All five LUMI operational tables are represented in the counts.
        assert set(result["details"]["perTableCounts"]) == {
            "rooms",
            "guests",
            "revenues",
            "reservations",
            "work_orders",
        }
        # The generated dataset is coherent: every table has items for the property.
        assert all(v > 0 for v in result["details"]["generatedCounts"].values())


class TestReconcileHandler:
    def test_returns_reconcile_envelope(self, mock_dataset_stack) -> None:
        import reconcile_handler as rec

        result = rec.lambda_handler(dict(ROLL_FORWARD_EVENT), CTX)
        _assert_envelope(result, "Reconcile")
        assert "reconciledCounts" in result["details"]


class TestUnQuiesceHandler:
    def test_returns_unquiesce_envelope(self) -> None:
        result = unquiesce_handler.lambda_handler(dict(ROLL_FORWARD_EVENT), CTX)
        _assert_envelope(result, "UnQuiesce")
        # Un-quiesce resumes evaluation, so quiesced must be False.
        assert result["details"]["quiesced"] is False

    def test_unquiesce_succeeds_on_bare_contract(self) -> None:
        # UnQuiesce is the Catch target; it must succeed given only the contract.
        result = unquiesce_handler.lambda_handler(
            {"mode": MODE_ROLL_FORWARD, "propertyId": "ALOHA-MIA-001"}, CTX
        )
        assert result["step"] == "UnQuiesce"
        assert result["status"] == STATUS_OK


class TestRegenerateBriefHandler:
    def test_returns_regenerate_envelope(self, mock_brief_regen) -> None:
        result = regenerate_brief_handler.lambda_handler(dict(ROLL_FORWARD_EVENT), CTX)
        _assert_envelope(result, "RegenerateBrief")
        assert result["details"]["briefsRequested"] == 1
        assert result["details"]["briefsSucceeded"] == 1
        assert result["details"]["briefsFailed"] == 0
        # generate-single was invoked once with the correct gmAlias/propertyId.
        assert len(mock_brief_regen.calls) == 1
        payload = json.loads(mock_brief_regen.calls[0]["Payload"].decode("utf-8"))
        assert payload["action"] == "generate-single"
        assert payload["gmAlias"] == "jsmith"
        assert payload["propertyId"] == "ALOHA-CHI-001"


class TestPrimeBaselineHandler:
    def test_returns_prime_baseline_envelope(self) -> None:
        # ALERTS_TABLE_NAME is intentionally unset in the orchestrator-only
        # environment, so the PULSE baseline seam degrades to a structured no-op
        # (zeroed counts) while still returning the standard envelope.
        result = prime_baseline_handler.lambda_handler(dict(ROLL_FORWARD_EVENT), CTX)
        _assert_envelope(result, "PrimeBaseline")
        assert "baselineAlertsPrimed" in result["details"]
        # Roll-forward scopes the baseline to exactly one property.
        assert result["details"]["targetProperties"] == ["ALOHA-CHI-001"]


class TestSeedModePrimeBaselineFanOut:
    """Seed mode fans the PrimeBaseline step out over the full pilot estate."""

    def test_seed_mode_accepted(self) -> None:
        result = prime_baseline_handler.lambda_handler(
            {"mode": MODE_SEED, "referenceDate": "2026-08-17"}, CTX
        )
        assert result["mode"] == MODE_SEED
        assert result["status"] == STATUS_OK
        # PrimeBaseline now sources the real pilot list from the generator config
        # (Task 5 wiring), so seed fan-out targets the whole pilot estate.
        assert result["details"]["targetProperties"] == PILOT


class TestSeedModeRegenerateFanOut:
    """Seed mode fans the RegenerateBrief step out over the full pilot estate."""

    def test_regenerate_seed_fans_out_over_estate(self, mock_brief_regen) -> None:
        result = regenerate_brief_handler.lambda_handler(
            {"mode": MODE_SEED, "referenceDate": "2026-08-17"}, CTX
        )
        assert result["mode"] == MODE_SEED
        assert result["status"] == STATUS_OK
        assert result["details"]["targetProperties"] == PILOT
        # One generate-single invoke per pilot property.
        assert len(mock_brief_regen.calls) == len(PILOT)


class TestSeedModeGenerateFanOut:
    """Seed mode fans the Generate step out over the full pilot estate."""

    def test_generate_seed_fans_out_over_estate(self, mock_dataset_stack) -> None:
        import generate_handler as gen

        result = gen.lambda_handler({"mode": MODE_SEED, "referenceDate": "2026-08-17"}, CTX)
        assert result["mode"] == MODE_SEED
        assert result["status"] == STATUS_OK
        assert result["details"]["targetProperties"] == PILOT

