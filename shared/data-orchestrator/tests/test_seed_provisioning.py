"""Unit tests for orchestrator seed-mode full first-deploy provisioning (Task 8).

Proves that the orchestrator ``mode: "seed"`` path drives the FULL first-deploy
seed by REUSING the existing idempotent LUMI/PULSE seed Lambdas (Requirement
1.3), and that it does so upsert-only - the invocation payload carries NO
``Force``/``ConfirmClear`` confirmation, so it can never trigger a destructive
clear (Requirements 8.1, 8.2). Also proves the roll-forward path does NOT invoke
application-data seeding.

The Lambda client and the generation runner are mocked - these tests never
touch AWS.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

import seed_provisioning
from orchestrator_common import StepInput


class _FakeResourceNotFound(Exception):
    """Stand-in for the boto3 ResourceNotFoundException."""


@pytest.fixture()
def fake_lambda_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the module-level Lambda client with a recording mock.

    Returns:
        The MagicMock standing in for the boto3 Lambda client.
    """
    client = MagicMock()
    client.exceptions = SimpleNamespace(ResourceNotFoundException=_FakeResourceNotFound)
    # A successful synchronous invoke: no FunctionError.
    client.invoke.return_value = {"StatusCode": 200, "FunctionError": None}
    monkeypatch.setattr(seed_provisioning, "_LAMBDA_CLIENT", client)
    return client


def _payloads_sent(client: MagicMock) -> List[Dict[str, Any]]:
    """Decode every JSON payload passed to the mocked ``invoke`` calls.

    Args:
        client: The mocked Lambda client.

    Returns:
        A list of decoded payload dicts, one per invoke call.
    """
    import json

    payloads: List[Dict[str, Any]] = []
    for call in client.invoke.call_args_list:
        payloads.append(json.loads(call.kwargs["Payload"].decode("utf-8")))
    return payloads


class TestProvisionApplicationSeed:
    """Direct tests of the reuse-not-duplicate provisioning helper."""

    def test_invokes_lumi_and_pulse_seed_lambdas(
        self, fake_lambda_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(seed_provisioning.ENV_SEED_LAMBDA_ARN, "arn:lumi-seed")
        monkeypatch.setenv(seed_provisioning.ENV_PULSE_SEED_LAMBDA_ARN, "arn:pulse-seed")

        detail = seed_provisioning.provision_application_seed()

        assert detail["lumiSeedInvoked"] is True
        assert detail["pulseSeedInvoked"] is True
        # Both existing seed Lambdas are reused (not re-implemented).
        assert fake_lambda_client.invoke.call_count == 2
        invoked_arns = {
            call.kwargs["FunctionName"] for call in fake_lambda_client.invoke.call_args_list
        }
        assert invoked_arns == {"arn:lumi-seed", "arn:pulse-seed"}

    def test_seed_payload_carries_no_destructive_confirmation(
        self, fake_lambda_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement 8.1/8.2: the automated seed must be upsert-only. The
        # payload must never carry Force or ConfirmClear.
        monkeypatch.setenv(seed_provisioning.ENV_SEED_LAMBDA_ARN, "arn:lumi-seed")
        monkeypatch.setenv(seed_provisioning.ENV_PULSE_SEED_LAMBDA_ARN, "arn:pulse-seed")

        seed_provisioning.provision_application_seed()

        for payload in _payloads_sent(fake_lambda_client):
            assert "Force" not in payload
            assert "ConfirmClear" not in payload
            assert payload["RequestType"] == "Create"

    def test_missing_lumi_arn_skips_gracefully(
        self, fake_lambda_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(seed_provisioning.ENV_SEED_LAMBDA_ARN, raising=False)
        monkeypatch.delenv(seed_provisioning.ENV_PULSE_SEED_LAMBDA_ARN, raising=False)

        detail = seed_provisioning.provision_application_seed()

        assert detail["lumiSeedInvoked"] is False
        assert detail["pulseSeedInvoked"] is False
        fake_lambda_client.invoke.assert_not_called()

    def test_unknown_arn_is_recorded_not_raised(
        self, fake_lambda_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(seed_provisioning.ENV_SEED_LAMBDA_ARN, "arn:missing")
        monkeypatch.delenv(seed_provisioning.ENV_PULSE_SEED_LAMBDA_ARN, raising=False)
        fake_lambda_client.invoke.side_effect = _FakeResourceNotFound()

        detail = seed_provisioning.provision_application_seed()

        # Graceful degradation: recorded, not raised (dataset seed still runs).
        assert detail["lumiSeedInvoked"] is False
        assert detail["lumiSeed"]["error"] == "ResourceNotFound"


class TestGenerateStepSeedRouting:
    """The Generate step drives application seed ONLY in seed mode."""

    def test_seed_mode_triggers_application_seed(self) -> None:
        import generate_handler

        step_input = StepInput(mode="seed", property_id=None, reference_date="2026-01-15")
        fake_gen = SimpleNamespace(
            per_table_counts={"rooms": 1},
            generated_counts={"rooms": 1},
        )
        with patch.object(
            generate_handler, "provision_application_seed", return_value={"lumiSeedInvoked": True}
        ) as mock_provision, patch.object(
            generate_handler, "run_generation", return_value=fake_gen
        ):
            detail = generate_handler.generate_window(
                step_input, pilot_property_ids=["ALOHA-CHI-001"]
            )

        mock_provision.assert_called_once()
        assert detail["applicationSeed"] == {"lumiSeedInvoked": True}

    def test_roll_forward_mode_skips_application_seed(self) -> None:
        import generate_handler

        step_input = StepInput(
            mode="roll-forward", property_id="ALOHA-CHI-001", reference_date="2026-01-15"
        )
        fake_gen = SimpleNamespace(
            per_table_counts={"rooms": 0},
            generated_counts={"rooms": 1},
        )
        with patch.object(
            generate_handler, "provision_application_seed"
        ) as mock_provision, patch.object(
            generate_handler, "run_generation", return_value=fake_gen
        ):
            detail = generate_handler.generate_window(
                step_input, pilot_property_ids=["ALOHA-CHI-001"]
            )

        # Roll-forward never touches application data (upsert-only dataset).
        mock_provision.assert_not_called()
        assert detail["applicationSeed"] == {}
