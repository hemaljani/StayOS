"""Shared pytest fixtures for the orchestrator handler tests.

Provides:

* A minimal fake Lambda context so the Powertools ``inject_lambda_context``
  decorator on each thin handler can run under unit test without a real Lambda
  runtime (mirrors the LUMI seed-data test pattern of passing a mock context).
* ``sys.path`` registration for the Task 1 ``dataset_generator`` package so the
  orchestrator handlers (which reuse those generators via
  ``dataset_generator_shim``) import cleanly under test without a hardcoded
  fragile path. This mirrors how the deployment packages the generators
  alongside the handlers at runtime.
* DynamoDB table-name environment variables matching
  ``lumi/backend/tests/conftest.py`` (PYQUALITY-06 / NAMING) plus dummy AWS
  credentials so moto-backed integration tests have a clean, deterministic
  environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# --- Make the handlers and the Task 1 dataset_generator importable ----------
# pytest.ini already puts ../handlers on the path; also register the LUMI
# seed-data directory that contains the dataset_generator package so the
# generators resolve during collection regardless of CWD.
_TESTS_DIR = Path(__file__).resolve().parent
_HANDLERS_DIR = _TESTS_DIR.parent / "handlers"
# tests -> data-orchestrator -> shared -> <repo root>
_REPO_ROOT = _TESTS_DIR.parents[2]
_SEED_DATA_DIR = _REPO_ROOT / "lumi" / "backend" / "functions" / "seed-data"

for _path in (str(_HANDLERS_DIR), str(_SEED_DATA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Point the shim's runtime env var at the seed-data dir so its preferred
# (env-var) resolution path is exercised, not just the repo-relative fallback.
os.environ.setdefault("DATASET_GENERATOR_PATH", str(_SEED_DATA_DIR))

# Physical table names for the 5 LUMI operational tables. Names match
# lumi/backend/tests/conftest.py so the generators and orchestrator agree.
DATASET_TABLE_ENV = {
    "ROOMS_TABLE_NAME": "stayos-rooms-test",
    "GUESTS_TABLE_NAME": "stayos-guests-test",
    "REVENUES_TABLE_NAME": "stayos-revenues-test",
    "RESERVATIONS_TABLE_NAME": "stayos-reservations-test",
    "WORK_ORDERS_TABLE_NAME": "stayos-work-orders-test",
}

# Key schema (PK, SK) per table, from docs/data-model.md, used by the moto
# integration fixture to create tables that match what the generators write.
DATASET_TABLE_KEYS = {
    "stayos-rooms-test": ("propertyId", "roomNumber"),
    "stayos-guests-test": ("propertyId", "guestId"),
    "stayos-revenues-test": ("propertyId", "date"),
    "stayos-reservations-test": ("propertyId", "dateReservationId"),
    "stayos-work-orders-test": ("propertyId", "workOrderId"),
}


@pytest.fixture(autouse=True)
def dataset_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dataset table names and dummy AWS credentials for every test.

    Autouse so any handler module reading table names from the environment
    finds valid values, and so boto3/moto never picks up real credentials.
    """
    for env_var, table_name in DATASET_TABLE_ENV.items():
        monkeypatch.setenv(env_var, table_name)
    monkeypatch.setenv("DATASET_GENERATOR_PATH", str(_SEED_DATA_DIR))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


def make_lambda_context() -> Any:
    """Return a minimal object with the attributes Powertools reads.

    Returns:
        A ``SimpleNamespace`` exposing ``function_name``,
        ``memory_limit_in_mb``, ``invoked_function_arn``, ``aws_request_id``,
        and ``log_stream_name``.
    """
    return SimpleNamespace(
        function_name="stayos-data-step",
        memory_limit_in_mb=256,
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:stayos-data-step",
        aws_request_id="test-request-id",
        log_stream_name="2026/08/17/[$LATEST]testtest",
    )


@pytest.fixture()
def lambda_context() -> Any:
    """Pytest fixture yielding a fresh fake Lambda context per test."""
    return make_lambda_context()


def create_dataset_tables(client: Any) -> None:
    """Create the 5 LUMI operational tables on a moto-backed client.

    Args:
        client: A moto-backed DynamoDB client.
    """
    for table_name, (partition_key, sort_key) in DATASET_TABLE_KEYS.items():
        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": partition_key, "KeyType": "HASH"},
                {"AttributeName": sort_key, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": partition_key, "AttributeType": "S"},
                {"AttributeName": sort_key, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=table_name)


def reload_wired_modules() -> None:
    """Reimport generator + handler modules so their boto3 clients are mocked.

    The generators create module-level DynamoDB clients at import time;
    reloading them while ``mock_aws`` is active rebinds those clients to moto.
    Reloads the generator writer/rooms modules and the shim/runner/handlers
    that reference them so an in-mock call routes through moto.
    """
    import importlib

    import dataset_generator.writer as writer_module
    import dataset_generator.rooms_generator as rooms_module

    importlib.reload(writer_module)
    importlib.reload(rooms_module)

    import dataset_generator_shim as shim_module
    import generation_runner as runner_module
    import generate_handler as generate_module
    import reconcile_handler as reconcile_module
    import local_runner as local_runner_module

    importlib.reload(shim_module)
    importlib.reload(runner_module)
    importlib.reload(generate_module)
    importlib.reload(reconcile_module)
    # local_runner binds the handler modules at import; reload it last so it
    # picks up the freshly reloaded generate/reconcile handlers.
    importlib.reload(local_runner_module)


@pytest.fixture()
def mock_dataset_stack() -> Any:
    """Stand up moto DynamoDB with the 5 tables and moto-backed wired modules.

    Yields:
        The moto-backed DynamoDB client. Generator/handler modules are reloaded
        inside the mock so their module-level clients route through moto.
    """
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_dataset_tables(client)
        reload_wired_modules()
        yield client
