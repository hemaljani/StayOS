"""Unit tests for the seed-data destructive-operation guard (Task 8).

Proves the two-factor, fail-closed guard on the bulk table-clear reseed
(Requirements 8.1, 8.2):

* The scheduled roll-forward path and CloudFormation Create/Update events NEVER
  invoke ``_clear_tables`` (upsert-only automated path).
* ``_clear_tables`` is reachable ONLY via an explicit manual invocation that
  carries BOTH ``Force == True`` AND a matching ``ConfirmClear`` confirmation
  token.
* A ``Force`` without the confirmation token is refused (fail-closed).

All external boundaries (Cognito, DynamoDB, EventBridge Scheduler, historical
briefs, dataset generators, and the CFN response URL) are mocked - these tests
never touch AWS and never actually clear a table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Import the seed-data lambda_function module explicitly by path to avoid a
# name collision with the api/lambda_function module (mirrors test_seed_data).
_SEED_DATA_DIR = str(Path(__file__).resolve().parents[2] / "functions" / "seed-data")


def _import_seed_lambda() -> Any:
    """Import the seed-data lambda_function module explicitly by path.

    Returns:
        The imported seed-data ``lambda_function`` module object.
    """
    spec = importlib.util.spec_from_file_location(
        "seed_lambda_function_guard",
        Path(_SEED_DATA_DIR) / "lambda_function.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def seed_lambda() -> Any:
    """Yield the freshly imported seed-data lambda module."""
    return _import_seed_lambda()


def _cfn_create_event(**overrides: Any) -> Dict[str, Any]:
    """Build a CloudFormation Create custom-resource event.

    Args:
        **overrides: Extra top-level fields to merge into the event (e.g. a
            ``Force`` flag or ``ConfirmClear`` token).

    Returns:
        A CloudFormation-shaped event dict.
    """
    event: Dict[str, Any] = {
        "RequestType": "Create",
        "ResponseURL": "https://cfn.example/response",
        "StackId": "stack",
        "RequestId": "req",
        "LogicalResourceId": "SeedDataCustomResource",
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# _is_clear_authorized - the pure two-factor guard
# ---------------------------------------------------------------------------


class TestIsClearAuthorized:
    """Direct unit tests of the fail-closed two-factor guard."""

    def test_scheduled_roll_forward_event_is_not_authorized(self, seed_lambda: Any) -> None:
        # A scheduled roll-forward event carries neither factor.
        event = {"mode": "roll-forward", "propertyId": "ALOHA-CHI-001"}
        assert seed_lambda._is_clear_authorized(event) is False

    def test_cfn_create_event_is_not_authorized(self, seed_lambda: Any) -> None:
        # A normal first-deploy Create event never clears tables.
        assert seed_lambda._is_clear_authorized(_cfn_create_event()) is False

    def test_force_without_confirmation_is_refused(self, seed_lambda: Any) -> None:
        # Fail-closed: Force alone (the old behavior) must NOT authorize a wipe.
        event = {seed_lambda.FORCE_FIELD: True}
        assert seed_lambda._is_clear_authorized(event) is False

    def test_force_with_wrong_token_is_refused(self, seed_lambda: Any) -> None:
        event = {
            seed_lambda.FORCE_FIELD: True,
            seed_lambda.CONFIRM_CLEAR_FIELD: "not-the-token",
        }
        assert seed_lambda._is_clear_authorized(event) is False

    def test_truthy_string_force_is_refused(self, seed_lambda: Any) -> None:
        # "Force": "false"/"true" strings must not slip through as boolean True.
        event = {
            seed_lambda.FORCE_FIELD: "true",
            seed_lambda.CONFIRM_CLEAR_FIELD: seed_lambda.CLEAR_CONFIRMATION_TOKEN,
        }
        assert seed_lambda._is_clear_authorized(event) is False

    def test_confirmation_token_without_force_is_refused(self, seed_lambda: Any) -> None:
        event = {seed_lambda.CONFIRM_CLEAR_FIELD: seed_lambda.CLEAR_CONFIRMATION_TOKEN}
        assert seed_lambda._is_clear_authorized(event) is False

    def test_manual_invoke_with_both_factors_is_authorized(self, seed_lambda: Any) -> None:
        # The ONLY authorized shape: explicit Force==True AND the exact token.
        event = {
            seed_lambda.FORCE_FIELD: True,
            seed_lambda.CONFIRM_CLEAR_FIELD: seed_lambda.CLEAR_CONFIRMATION_TOKEN,
        }
        assert seed_lambda._is_clear_authorized(event) is True


# ---------------------------------------------------------------------------
# lambda_handler - end-to-end proof that _clear_tables is only reached via
# an authorized manual invoke, and never on the automated path.
# ---------------------------------------------------------------------------


def _patched_handler_run(seed_lambda: Any, event: Dict[str, Any]) -> MagicMock:
    """Run the handler with every external boundary mocked; return the clear mock.

    Patches provisioning, historical briefs, dataset generators, the CFN
    response, and ``_clear_tables`` so no AWS call is made and no table is
    actually cleared. Forces the "tables empty" branch so generation would run
    were it not mocked, ensuring the ONLY thing gating ``_clear_tables`` is the
    guard under test.

    Args:
        seed_lambda: The imported seed-data lambda module.
        event: The invocation event.

    Returns:
        The ``_clear_tables`` MagicMock, for call assertions.
    """
    context = SimpleNamespace(
        function_name="stayos-seed-data",
        memory_limit_in_mb=256,
        invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:stayos-seed-data",
        aws_request_id="test-request-id",
        log_stream_name="2026/08/17/[$LATEST]guardtest",
    )

    with patch.object(seed_lambda, "provision_cognito_users", return_value=5), \
        patch.object(seed_lambda, "seed_settings_table", return_value=5), \
        patch.object(seed_lambda, "provision_schedules", return_value=5), \
        patch.object(seed_lambda, "seed_historical_briefs", return_value=7), \
        patch.object(seed_lambda, "generate_rooms", return_value={}), \
        patch.object(seed_lambda, "generate_guests", return_value={}), \
        patch.object(seed_lambda, "generate_revenue", return_value={}), \
        patch.object(seed_lambda, "generate_reservations", return_value=[]), \
        patch.object(seed_lambda, "generate_work_orders", return_value=[]), \
        patch.object(seed_lambda, "reconcile_room_status", return_value={}), \
        patch.object(seed_lambda, "BatchWriter", MagicMock()), \
        patch.object(seed_lambda, "send_cfn_response"), \
        patch.object(seed_lambda, "_tables_already_seeded", return_value=False), \
        patch.object(seed_lambda, "_clear_tables", return_value=0) as mock_clear:
        seed_lambda.lambda_handler(event, context)

    return mock_clear


class TestHandlerNeverClearsOnAutomatedPath:
    """The scheduled/automated path must be upsert-only (Requirement 8.1)."""

    def test_scheduled_roll_forward_never_clears(self, seed_lambda: Any) -> None:
        # A scheduled-style Create event with no confirmation must never clear.
        event = _cfn_create_event(mode="roll-forward", propertyId="ALOHA-CHI-001")
        mock_clear = _patched_handler_run(seed_lambda, event)
        mock_clear.assert_not_called()

    def test_plain_create_never_clears(self, seed_lambda: Any) -> None:
        mock_clear = _patched_handler_run(seed_lambda, _cfn_create_event())
        mock_clear.assert_not_called()

    def test_force_only_without_token_never_clears(self, seed_lambda: Any) -> None:
        # Old behavior (Force alone) is now fail-closed at the handler too.
        event = _cfn_create_event(Force=True)
        mock_clear = _patched_handler_run(seed_lambda, event)
        mock_clear.assert_not_called()


class TestHandlerClearsOnlyWithConfirmation:
    """Force clear-tables triggers ONLY via authorized manual invoke (8.2)."""

    def test_manual_invoke_with_confirmation_clears(self, seed_lambda: Any) -> None:
        event = _cfn_create_event(
            Force=True,
            ConfirmClear=seed_lambda.CLEAR_CONFIRMATION_TOKEN,
        )
        mock_clear = _patched_handler_run(seed_lambda, event)
        mock_clear.assert_called_once()
