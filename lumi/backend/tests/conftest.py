"""Shared pytest fixtures for LUMI backend tests."""

import importlib
import sys
from unittest.mock import Mock

import pytest

# The dataset_generator submodules that must resolve to their REAL
# implementations for every test. Older test modules stubbed these with
# ``MagicMock`` at import time; those stubs leaked across files via
# ``sys.modules`` and caused order-dependent failures. This list drives an
# autouse fixture that evicts any leaked mock and re-imports the real module
# before each test runs.
_DATASET_GENERATOR_MODULES = (
    "dataset_generator",
    "dataset_generator.config",
    "dataset_generator.reference_date",
    "dataset_generator.rooms_generator",
    "dataset_generator.guests_generator",
    "dataset_generator.revenue_generator",
    "dataset_generator.reservations_generator",
    "dataset_generator.work_orders_generator",
    "dataset_generator.writer",
)


@pytest.fixture(autouse=True)
def real_dataset_generator_modules() -> None:
    """Ensure real dataset_generator modules are loaded before each test.

    Some legacy test modules previously replaced dataset_generator submodules
    with ``unittest.mock.Mock``/``MagicMock`` instances in ``sys.modules`` at
    import time. Because ``sys.modules`` is process-global, those stubs leaked
    into unrelated test files and produced order-dependent failures (a file
    passing in isolation but failing when run alongside others).

    This fixture runs before every test, evicts any mock instance found under
    the known dataset_generator keys, and re-imports the genuine module so each
    test observes the real implementation regardless of collection order.
    """
    for module_name in _DATASET_GENERATOR_MODULES:
        cached = sys.modules.get(module_name)
        if isinstance(cached, Mock):
            # Drop the leaked mock so importlib re-resolves the real module.
            del sys.modules[module_name]
        try:
            importlib.import_module(module_name)
        except ImportError:
            # Module may be genuinely unavailable in some environments; skip it
            # rather than failing the whole suite.
            continue


@pytest.fixture(autouse=True)
def aws_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set mock AWS environment variables for all tests.

    This fixture ensures Lambda function code that reads environment
    variables at module level will find valid values during testing.
    """
    monkeypatch.setenv("BRIEFS_TABLE_NAME", "stayos-briefs-test")
    monkeypatch.setenv("SETTINGS_TABLE_NAME", "stayos-settings-test")
    monkeypatch.setenv("AUDIO_BUCKET_NAME", "stayos-audio-test-000000000000")
    monkeypatch.setenv("AUDIO_CLOUDFRONT_DOMAIN", "d1234567890.cloudfront.net")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_TestPool")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.claude-3-5-sonnet-20241022-v2:0")
    monkeypatch.setenv("SPOG_API_ENDPOINT", "https://spog-api.test.example.com")
    monkeypatch.setenv("MDP_API_ENDPOINT", "https://mdp-api.test.example.com")
    monkeypatch.setenv("SPOG_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:000000000000:secret:lumi/spog/api-key")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://test.cloudfront.net")
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("RESERVATIONS_TABLE_NAME", "stayos-reservations-test")
    monkeypatch.setenv("ROOMS_TABLE_NAME", "stayos-rooms-test")
    monkeypatch.setenv("GUESTS_TABLE_NAME", "stayos-guests-test")
    monkeypatch.setenv("REVENUES_TABLE_NAME", "stayos-revenues-test")
    monkeypatch.setenv("WORK_ORDERS_TABLE_NAME", "stayos-work-orders-test")
    monkeypatch.setenv("DEMO_PASSWORD", "TestPassword123!")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("SCHEDULE_GROUP_NAME", "stayos-briefs")
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::000000000000:role/stayos-scheduler-role")
    monkeypatch.setenv("ORCHESTRATOR_ARN", "arn:aws:lambda:us-east-1:000000000000:function:stayos-orchestrator")
    monkeypatch.setenv("STACK_PREFIX", "lumi")

