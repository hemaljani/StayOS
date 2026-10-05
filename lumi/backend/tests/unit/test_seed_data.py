"""Unit tests for LUMI seed data Lambda function.

Tests Cognito user provisioning, DynamoDB settings seeding, idempotency
behavior, and the CloudFormation custom resource handler flow.
"""

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import boto3
import moto
import pytest

from seed_data import (
    GM_SEED_DATA,
    provision_cognito_users,
    seed_settings_table,
)
from seed_data import DEMO_PASSWORD_ENV, _get_demo_password

# Import seed-data lambda_function explicitly to avoid collision with api/lambda_function
_SEED_DATA_DIR = str(Path(__file__).resolve().parents[2] / "functions" / "seed-data")


def _import_seed_lambda():
    """Import the seed-data lambda_function module explicitly by path."""
    spec = importlib.util.spec_from_file_location(
        "seed_lambda_function",
        Path(_SEED_DATA_DIR) / "lambda_function.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cognito_user_pool() -> str:
    """Create a mocked Cognito User Pool and return its ID.

    Sets up custom attributes (propertyId, gmAlias) matching the LUMI
    User Pool schema defined in auth.yaml.

    Returns:
        The mocked User Pool ID.
    """
    with moto.mock_aws():
        client = boto3.client("cognito-idp", region_name="us-east-1")
        response = client.create_user_pool(
            PoolName="stayos-users-test",
            Schema=[
                {
                    "Name": "propertyId",
                    "AttributeDataType": "String",
                    "Mutable": False,
                },
                {
                    "Name": "gmAlias",
                    "AttributeDataType": "String",
                    "Mutable": False,
                },
            ],
        )
        yield response["UserPool"]["Id"]


@pytest.fixture
def dynamodb_settings_table() -> str:
    """Create a mocked DynamoDB settings table and return its name.

    Table schema matches stayos-settings with gmAlias as partition key.

    Returns:
        The mocked table name.
    """
    table_name = "stayos-settings-test"
    with moto.mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "gmAlias", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "gmAlias", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table_name


@pytest.fixture
def cfn_create_event() -> Dict[str, Any]:
    """Generate a CloudFormation Create custom resource event.

    Returns:
        Dictionary matching the structure CloudFormation sends to Lambda.
    """
    return {
        "RequestType": "Create",
        "ResponseURL": "https://cloudformation-custom-resource-response.s3.amazonaws.com/test-response",
        "StackId": "arn:aws:cloudformation:us-east-1:000000000000:stack/stayos-us-east-1/12345",
        "RequestId": "unique-request-id-001",
        "LogicalResourceId": "SeedDataCustomResource",
        "ResourceType": "Custom::SeedData",
        "ResourceProperties": {
            "ServiceToken": "arn:aws:lambda:us-east-1:000000000000:function:stayos-seed-data",
            "Trigger": "v1",
        },
    }


@pytest.fixture
def cfn_delete_event(cfn_create_event: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a CloudFormation Delete custom resource event.

    Args:
        cfn_create_event: Base event to modify.

    Returns:
        Dictionary with RequestType set to Delete.
    """
    event = cfn_create_event.copy()
    event["RequestType"] = "Delete"
    return event


# ---------------------------------------------------------------------------
# Tests: provision_cognito_users
# ---------------------------------------------------------------------------


@moto.mock_aws
def test_provision_cognito_users_creates_all_users() -> None:
    """Verify all 5 GM users are created in Cognito with correct attributes."""
    # Set up the mocked User Pool with required custom attributes
    client = boto3.client("cognito-idp", region_name="us-east-1")
    pool_response = client.create_user_pool(
        PoolName="stayos-users-test",
        Schema=[
            {"Name": "propertyId", "AttributeDataType": "String", "Mutable": False},
            {"Name": "gmAlias", "AttributeDataType": "String", "Mutable": False},
        ],
    )
    user_pool_id = pool_response["UserPool"]["Id"]

    # Patch the module-level client so provision_cognito_users uses the mock
    with patch("seed_data._cognito_client", client):
        users_created = provision_cognito_users(
            user_pool_id=user_pool_id,
            gm_list=GM_SEED_DATA,
        )

    # Verify all 5 users were created
    assert users_created == 5

    # Verify users exist in the pool with correct attributes
    list_response = client.list_users(UserPoolId=user_pool_id)
    assert len(list_response["Users"]) == 5

    # Spot-check first user attributes
    first_user = next(
        user
        for user in list_response["Users"]
        if user["Username"] == "jsmith@aloha.com"
    )
    attrs = {attr["Name"]: attr["Value"] for attr in first_user["Attributes"]}
    assert attrs["custom:propertyId"] == "ALOHA-CHI-001"
    assert attrs["custom:gmAlias"] == "jsmith"
    assert attrs["email"] == "jsmith@aloha.com"
    # Full display name is written to the standard Cognito "name" attribute so
    # it rides in the idToken and the shell can greet the GM by name.
    assert attrs["name"] == "Jennifer Smith"


@moto.mock_aws
def test_provision_cognito_users_handles_existing_user() -> None:
    """Verify existing users are skipped without error (idempotent behavior)."""
    client = boto3.client("cognito-idp", region_name="us-east-1")
    pool_response = client.create_user_pool(
        PoolName="stayos-users-test",
        Schema=[
            {"Name": "propertyId", "AttributeDataType": "String", "Mutable": False},
            {"Name": "gmAlias", "AttributeDataType": "String", "Mutable": False},
        ],
    )
    user_pool_id = pool_response["UserPool"]["Id"]

    # Pre-create one user to simulate an existing account
    client.admin_create_user(
        UserPoolId=user_pool_id,
        Username="jsmith@aloha.com",
        UserAttributes=[
            {"Name": "email", "Value": "jsmith@aloha.com"},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "custom:propertyId", "Value": "ALOHA-CHI-001"},
            {"Name": "custom:gmAlias", "Value": "jsmith"},
        ],
        MessageAction="SUPPRESS",
    )

    with patch("seed_data._cognito_client", client):
        users_created = provision_cognito_users(
            user_pool_id=user_pool_id,
            gm_list=GM_SEED_DATA,
        )

    # Only 4 new users should be created (jsmith already existed)
    assert users_created == 4

    # Total users should still be 5
    list_response = client.list_users(UserPoolId=user_pool_id)
    assert len(list_response["Users"]) == 5


# ---------------------------------------------------------------------------
# Tests: seed_settings_table
# ---------------------------------------------------------------------------


@moto.mock_aws
def test_seed_settings_table_creates_records() -> None:
    """Verify all 5 GM settings records are created with correct attributes."""
    table_name = "stayos-settings-test"
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    with patch("seed_data._dynamodb_resource", dynamodb):
        records_seeded = seed_settings_table(
            table_name=table_name,
            gm_list=GM_SEED_DATA,
        )

    assert records_seeded == 5

    # Verify record structure for first GM
    table = dynamodb.Table(table_name)
    response = table.get_item(Key={"gmAlias": "jsmith"})
    item = response["Item"]

    assert item["propertyId"] == "ALOHA-CHI-001"
    assert item["briefDeliveryTime"] == "06:30"
    assert item["alertToggles"]["overbookingRisk"] is True
    assert item["alertToggles"]["vipArrivalAlert"] is True
    assert item["kpiThresholds"]["occupancyAlertBelow"] == 70
    assert item["kpiThresholds"]["adrAlertBelow"] == 200
    assert item["audioPreferences"]["language"] == "en-US"
    assert item["audioPreferences"]["briefLength"] == "standard"
    assert "createdAt" in item
    assert "updatedAt" in item

    # Verify Japanese GM gets ja-JP language
    jp_response = table.get_item(Key={"gmAlias": "ttanaka"})
    assert jp_response["Item"]["audioPreferences"]["language"] == "ja-JP"

    # Verify Spanish GM gets es-ES language
    es_response = table.get_item(Key={"gmAlias": "cgarcia"})
    assert es_response["Item"]["audioPreferences"]["language"] == "es-ES"


@moto.mock_aws
def test_seed_settings_table_idempotent() -> None:
    """Verify running seed twice does not overwrite existing records."""
    table_name = "stayos-settings-test"
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    with patch("seed_data._dynamodb_resource", dynamodb):
        # First run - all records created
        first_run_count = seed_settings_table(
            table_name=table_name,
            gm_list=GM_SEED_DATA,
        )
        assert first_run_count == 5

        # Get the original createdAt timestamp for comparison
        table = dynamodb.Table(table_name)
        original_item = table.get_item(Key={"gmAlias": "jsmith"})["Item"]
        original_created_at = original_item["createdAt"]

        # Second run - no records should be created (all exist)
        second_run_count = seed_settings_table(
            table_name=table_name,
            gm_list=GM_SEED_DATA,
        )
        assert second_run_count == 0

        # Verify original record was not modified
        unchanged_item = table.get_item(Key={"gmAlias": "jsmith"})["Item"]
        assert unchanged_item["createdAt"] == original_created_at


# ---------------------------------------------------------------------------
# Tests: lambda_handler
# ---------------------------------------------------------------------------


@moto.mock_aws
def test_lambda_handler_create_event(
    cfn_create_event: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify handler provisions users and settings on Create event."""
    # Set up mocked AWS resources
    cognito_client = boto3.client("cognito-idp", region_name="us-east-1")
    pool_response = cognito_client.create_user_pool(
        PoolName="stayos-users-test",
        Schema=[
            {"Name": "propertyId", "AttributeDataType": "String", "Mutable": False},
            {"Name": "gmAlias", "AttributeDataType": "String", "Mutable": False},
        ],
    )
    user_pool_id = pool_response["UserPool"]["Id"]

    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Set environment variables the handler reads
    monkeypatch.setenv("COGNITO_USER_POOL_ID", user_pool_id)
    monkeypatch.setenv("SETTINGS_TABLE_NAME", "stayos-settings-test")

    # Import the seed-data lambda_function explicitly to avoid collision with api/lambda_function
    seed_lambda = _import_seed_lambda()

    # Mock the cfn response URL call (we can't actually PUT to S3 presigned URL)
    with patch("seed_data._cognito_client", cognito_client), \
         patch("seed_data._dynamodb_resource", dynamodb), \
         patch.object(seed_lambda, "send_cfn_response") as mock_send_response:

        mock_context = MagicMock()
        mock_context.function_name = "stayos-seed-data"

        seed_lambda.lambda_handler(cfn_create_event, mock_context)

        # Verify CloudFormation was notified of success
        mock_send_response.assert_called_once()
        call_kwargs = mock_send_response.call_args
        assert call_kwargs[1]["status"] == "SUCCESS"
        assert "5" in call_kwargs[1]["reason"] or "Provisioned" in call_kwargs[1]["reason"]


@moto.mock_aws
def test_lambda_handler_delete_event(
    cfn_delete_event: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify handler is a no-op on Delete event and sends SUCCESS."""
    # Import the seed-data lambda_function explicitly to avoid collision with api/lambda_function
    seed_lambda = _import_seed_lambda()

    with patch.object(seed_lambda, "send_cfn_response") as mock_send_response:
        mock_context = MagicMock()
        mock_context.function_name = "stayos-seed-data"

        seed_lambda.lambda_handler(cfn_delete_event, mock_context)

        # Verify CloudFormation was notified of success (no-op)
        mock_send_response.assert_called_once()
        call_kwargs = mock_send_response.call_args
        assert call_kwargs[1]["status"] == "SUCCESS"
        assert "no-op" in call_kwargs[1]["reason"].lower() or "preserved" in call_kwargs[1]["reason"].lower()


# ---------------------------------------------------------------------------
# Tests: GM_SEED_DATA integrity
# ---------------------------------------------------------------------------


def test_gm_seed_data_has_5_entries() -> None:
    """Verify the seed data constant contains exactly 5 GM records."""
    assert len(GM_SEED_DATA) == 5


def test_gm_seed_data_unique_aliases() -> None:
    """Verify all gmAlias values are unique across the seed data."""
    aliases = [gm["gmAlias"] for gm in GM_SEED_DATA]
    assert len(aliases) == len(set(aliases))


def test_gm_seed_data_unique_property_ids() -> None:
    """Verify all propertyId values are unique across the seed data."""
    property_ids = [gm["propertyId"] for gm in GM_SEED_DATA]
    assert len(property_ids) == len(set(property_ids))


def test_gm_seed_data_required_fields() -> None:
    """Verify each GM record has all required fields."""
    required_fields = {
        "gmAlias",
        "gmName",
        "email",
        "propertyId",
        "propertyName",
        "brand",
        "city",
        "timezone",
        "totalRooms",
        "language",
    }
    for gm in GM_SEED_DATA:
        missing = required_fields - set(gm.keys())
        assert not missing, f"GM {gm.get('gmAlias', 'unknown')} missing fields: {missing}"


def test_gm_seed_data_region_distribution() -> None:
    """Verify 5 GMs span 4 regions (US, Japan, Europe, India)."""
    us_timezones = {"America/Chicago", "America/New_York", "America/Los_Angeles"}
    japan_timezones = {"Asia/Tokyo"}
    europe_timezones = {"Europe/London", "Europe/Madrid", "Europe/Berlin", "Europe/Paris", "Europe/Rome"}
    india_timezones = {"Asia/Kolkata"}

    us_count = sum(1 for gm in GM_SEED_DATA if gm["timezone"] in us_timezones)
    japan_count = sum(1 for gm in GM_SEED_DATA if gm["timezone"] in japan_timezones)
    europe_count = sum(1 for gm in GM_SEED_DATA if gm["timezone"] in europe_timezones)
    india_count = sum(1 for gm in GM_SEED_DATA if gm["timezone"] in india_timezones)

    assert us_count == 2, f"Expected 2 US properties, got {us_count}"
    assert japan_count == 1, f"Expected 1 Japan property, got {japan_count}"
    assert europe_count == 1, f"Expected 1 Europe property, got {europe_count}"
    assert india_count == 1, f"Expected 1 India property, got {india_count}"



# ---------------------------------------------------------------------------
# _get_demo_password (Env-1: lazy DEMO_PASSWORD read)
# ---------------------------------------------------------------------------


class TestGetDemoPassword:
    """The demo password is read lazily from the environment at call time."""

    def test_returns_env_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When DEMO_PASSWORD is set, the value is returned verbatim."""
        monkeypatch.setenv(DEMO_PASSWORD_ENV, "SuperSecret!123")
        assert _get_demo_password() == "SuperSecret!123"

    def test_raises_keyerror_with_context_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When DEMO_PASSWORD is absent, a clear KeyError is raised at call time.

        Env-1: reading lazily means module import never requires the variable,
        but the one function that needs it fails loudly (naming AppPassword) if
        it is missing at runtime.
        """
        monkeypatch.delenv(DEMO_PASSWORD_ENV, raising=False)
        with pytest.raises(KeyError) as exc_info:
            _get_demo_password()
        assert DEMO_PASSWORD_ENV in str(exc_info.value)
        assert "AppPassword" in str(exc_info.value)
