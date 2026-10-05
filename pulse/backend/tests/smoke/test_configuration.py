"""Smoke tests over PULSE one-time configuration (Task 23.4).

Single-execution assertions on the CloudFormation templates and the default
rule-template seeding:

    * ``pulse-rules`` key schema is (propertyId HASH, ruleType RANGE) (2.1),
    * default rule templates seed ``enabled=true`` for every alert-producing
      type (2.2),
    * the LUMI Streams enablement sets ``NEW_AND_OLD_IMAGES`` on the five
      operational tables,
    * the observability dashboard carries per-tier latency widgets + alarms
      (17.2),
    * ``pulse-alert-history`` has the ``expiresAt`` TTL / 90-day retention
      (14.5, 14.6),
    * the ``pulse-api`` Cognito JWT authorizer is wired to the LUMI user pool
      (16.1),
    * the AppSync Events API has the ``pulse`` namespace, Cognito auth,
      OnSubscribe/OnPublish handlers, logging, and a WAF association.
"""

from __future__ import annotations

from typing import Any

from pulse.common.models import AlertType
from pulse.history import writer
from pulse.rule_engine.rule_validation import default_rule_templates

# The five LUMI operational tables whose Streams PULSE consumes.
_LUMI_STREAM_TABLES = {
    "reservations",
    "rooms",
    "guests",
    "revenues",
    "work-orders",
}


def _sub_value(node: Any) -> str:
    """Return the string body of a ``!Sub`` node (or a plain string).

    Args:
        node: A parsed template value that may be ``{"Sub": "..."}`` or a str.

    Returns:
        The underlying string.
    """
    if isinstance(node, dict) and "Sub" in node:
        body = node["Sub"]
        return body[0] if isinstance(body, list) else str(body)
    return str(node)


def _tables_of_type(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return DynamoDB table resources keyed by their physical name suffix.

    Args:
        template: A parsed CloudFormation template.

    Returns:
        A mapping of ``${StackPrefix}-<suffix>`` -> resource properties for
        every ``AWS::DynamoDB::Table`` resource.
    """
    tables: dict[str, dict[str, Any]] = {}
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") != "AWS::DynamoDB::Table":
            continue
        props = resource.get("Properties", {})
        name = _sub_value(props.get("TableName", ""))
        tables[name] = props
    return tables


def _key_schema(properties: dict[str, Any]) -> dict[str, str]:
    """Return a ``{KeyType: AttributeName}`` mapping for a table's key schema."""
    return {
        entry["KeyType"]: entry["AttributeName"]
        for entry in properties.get("KeySchema", [])
    }


# ---------------------------------------------------------------------------
# Requirement 2.1 - pulse-rules key schema
# ---------------------------------------------------------------------------


def test_pulse_rules_key_schema(pulse_data_template: dict[str, Any]) -> None:
    """pulse-rules is keyed by (propertyId HASH, ruleType RANGE).

    Validates: Requirement 2.1
    """
    rules_table = pulse_data_template["Resources"]["PulseRulesTable"]["Properties"]
    assert _sub_value(rules_table["TableName"]) == "${StackPrefix}-rules"
    schema = _key_schema(rules_table)
    assert schema["HASH"] == "propertyId"
    assert schema["RANGE"] == "ruleType"


# ---------------------------------------------------------------------------
# pulse-kitchen key schema (propertyId HASH only, one item per property)
# ---------------------------------------------------------------------------


def test_pulse_kitchen_key_schema(pulse_data_template: dict[str, Any]) -> None:
    """pulse-kitchen is keyed by propertyId HASH only (one snapshot per property)."""
    kitchen_table = pulse_data_template["Resources"]["PulseKitchenTable"]["Properties"]
    assert _sub_value(kitchen_table["TableName"]) == "${StackPrefix}-kitchen"
    schema = _key_schema(kitchen_table)
    assert schema["HASH"] == "propertyId"
    assert "RANGE" not in schema
    assert kitchen_table["BillingMode"] == "PAY_PER_REQUEST"


# ---------------------------------------------------------------------------
# Kitchen seed custom resource (mirrors LUMI Custom::SeedData)
# ---------------------------------------------------------------------------


def test_kitchen_seed_custom_resource_wired(
    pulse_data_template: dict[str, Any],
) -> None:
    """pulse-data wires a Custom::KitchenSeed resource to the seed Lambda.

    Asserts the three mirrored pieces exist and are connected: the seed Lambda
    function (shared package, correct handler + env), a least-privilege role
    scoped to only the kitchen table, and the custom resource whose
    ServiceToken is the seed function and which carries a Trigger property.
    """
    resources = pulse_data_template["Resources"]

    # (1) Seed Lambda: shared deployment package + CFN custom-resource handler.
    fn = resources["KitchenSeedFunction"]["Properties"]
    assert _sub_value(fn["FunctionName"]) == "${StackPrefix}-kitchen-seed"
    assert fn["Handler"] == "pulse.seed.kitchen_seed.lambda_handler"
    assert fn["Runtime"] == "python3.12"
    assert fn["Code"]["S3Key"] == {"Ref": "LambdaCodeS3Key"}
    assert fn["Environment"]["Variables"]["KITCHEN_TABLE_NAME"] == {
        "Ref": "PulseKitchenTable"
    }
    # Seeds the whole pilot estate (comma-separated) so every GM's property has
    # a snapshot and no property 404s on the Kitchen tab.
    assert "ESTATE_PROPERTY_IDS" in fn["Environment"]["Variables"]

    # (2) Role: PutItem + GetItem/Scan/Query scoped to ONLY the kitchen table ARN.
    role = resources["KitchenSeedLambdaRole"]["Properties"]
    assert _sub_value(role["RoleName"]) == "${StackPrefix}-kitchen-seed-role-${AWS::Region}"
    seed_stmt = next(
        policy["PolicyDocument"]["Statement"][0]
        for policy in role["Policies"]
        if policy["PolicyName"] == "KitchenSeedWrite"
    )
    assert set(seed_stmt["Action"]) == {
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:Scan",
        "dynamodb:Query",
    }
    assert seed_stmt["Resource"] == {"GetAtt": "PulseKitchenTable.Arn"}

    # (3) Custom resource: ServiceToken -> seed fn, plus a Trigger property.
    seed_cr = resources["KitchenSeedCustomResource"]
    assert seed_cr["Type"] == "Custom::KitchenSeed"
    assert seed_cr["Properties"]["ServiceToken"] == {
        "GetAtt": "KitchenSeedFunction.Arn"
    }
    assert "Trigger" in seed_cr["Properties"]


# ---------------------------------------------------------------------------
# Requirement 2.2 - default rule templates seed enabled=true
# ---------------------------------------------------------------------------


def test_default_rule_templates_seed_enabled_true() -> None:
    """Every seeded default rule template is enabled for its alert type.

    Note:
        ``default_rule_templates`` seeds one template per alert-*producing* type
        (UC-01..UC-06). Requirement 2.2 phrases this as "eight MVP alert types",
        but UC-07 (escalation routing) and UC-08 (history/shift-handover) are
        platform capabilities, not rule-driven alert types, so they carry no
        template (documented in ``rule_validation.default_rule_templates``).

    Validates: Requirement 2.2
    """
    templates = default_rule_templates("ALOHA-CHI-001")

    assert templates, "expected default rule templates to be seeded"
    assert all(template.enabled is True for template in templates)

    seeded_types = {template.rule_type for template in templates}
    expected_types = {
        AlertType.WALK_RISK,
        AlertType.VIP_ROOM_NOT_READY,
        AlertType.COMPLAINT_ESCALATION,
        AlertType.OOO_CLUSTER,
        AlertType.PREMIUM_CANCELLATION,
        AlertType.VIP_CHECKIN,
    }
    assert seeded_types == expected_types


# ---------------------------------------------------------------------------
# Streams enablement - NEW_AND_OLD_IMAGES on the five operational tables
# ---------------------------------------------------------------------------


def test_lumi_operational_tables_have_new_and_old_images_stream(
    lumi_data_template: dict[str, Any],
) -> None:
    """All five LUMI operational tables emit NEW_AND_OLD_IMAGES streams.

    Validates: Requirement 1.1 (Streams enablement, Decision 4)
    """
    tables = _tables_of_type(lumi_data_template)
    for suffix in _LUMI_STREAM_TABLES:
        physical = f"${{StackPrefix}}-{suffix}"
        assert physical in tables, f"missing operational table {physical}"
        stream_spec = tables[physical].get("StreamSpecification", {})
        assert stream_spec.get("StreamViewType") == "NEW_AND_OLD_IMAGES", (
            f"{physical} must stream NEW_AND_OLD_IMAGES"
        )


# ---------------------------------------------------------------------------
# Requirement 17.2 - observability dashboard widgets + alarms
# ---------------------------------------------------------------------------


def test_observability_dashboard_has_per_tier_latency_widgets(
    pulse_observability_template: dict[str, Any],
) -> None:
    """The overview dashboard renders p50/p90/p99 latency per tier.

    Validates: Requirement 17.2
    """
    resources = pulse_observability_template["Resources"]
    dashboard = resources["PulseOverviewDashboard"]["Properties"]
    body = _sub_value(dashboard["DashboardBody"])

    assert "AlertDeliveryLatencyMs" in body
    for tier in ("CRITICAL", "WARNING", "INFO"):
        assert tier in body, f"dashboard missing {tier} latency widget"
    for percentile in ("p50", "p90", "p99"):
        assert percentile in body, f"dashboard missing {percentile} statistic"


def test_observability_has_pipeline_failure_alarms(
    pulse_observability_template: dict[str, Any],
) -> None:
    """The stack defines the pipeline-failure alarms wired to the SNS topic.

    Validates: Requirement 17.2 (alarms)
    """
    resources = pulse_observability_template["Resources"]
    alarm_names = {
        _sub_value(resource["Properties"]["AlarmName"])
        for resource in resources.values()
        if resource.get("Type") == "AWS::CloudWatch::Alarm"
    }
    assert "${StackPrefix}-triage-failures-high" in alarm_names
    assert "${StackPrefix}-delivery-exhausted-high" in alarm_names
    assert "${StackPrefix}-escalation-exhaustion-high" in alarm_names


# ---------------------------------------------------------------------------
# Requirements 14.5, 14.6 - pulse-alert-history expiresAt TTL / 90-day retention
# ---------------------------------------------------------------------------


def test_pulse_alert_history_ttl(pulse_data_template: dict[str, Any]) -> None:
    """pulse-alert-history has the expiresAt TTL and a 90-day retention window.

    Validates: Requirements 14.5, 14.6
    """
    history = pulse_data_template["Resources"]["PulseAlertHistoryTable"]["Properties"]
    ttl = history["TimeToLiveSpecification"]
    assert ttl["AttributeName"] == "expiresAt"
    assert ttl["Enabled"] is True
    # The 90-day retention itself is applied by the writer (expiresAt = created + 90d).
    assert writer.HISTORY_RETENTION_DAYS == 90


# ---------------------------------------------------------------------------
# Requirement 16.1 - REST Cognito JWT authorizer wired to the LUMI user pool
# ---------------------------------------------------------------------------


def test_rest_cognito_jwt_authorizer_uses_lumi_user_pool(
    pulse_api_template: dict[str, Any],
) -> None:
    """The HTTP API JWT authorizer issuer references the LUMI user pool.

    Validates: Requirement 16.1
    """
    authorizer = pulse_api_template["Resources"]["PulseApiJwtAuthorizer"]["Properties"]
    assert authorizer["AuthorizerType"] == "JWT"
    issuer = _sub_value(authorizer["JwtConfiguration"]["Issuer"])
    assert "cognito-idp" in issuer
    assert "${UserPoolId}" in issuer


# ---------------------------------------------------------------------------
# AppSync Events realtime config (pulse namespace, Cognito auth, handlers,
# logging, WAF association)
# ---------------------------------------------------------------------------


def test_appsync_events_api_cognito_auth_and_logging(
    pulse_api_template: dict[str, Any],
) -> None:
    """The AppSync Events API uses Cognito auth and enables CloudWatch logging.

    Validates: AppSync Events configuration
    """
    event_api = pulse_api_template["Resources"]["PulseRealtimeEventApi"]["Properties"]
    event_config = event_api["EventConfig"]

    auth_types = {
        provider["AuthType"] for provider in event_config["AuthProviders"]
    }
    assert "AMAZON_COGNITO_USER_POOLS" in auth_types

    cognito_config = event_config["AuthProviders"][0]["CognitoConfig"]
    assert cognito_config["UserPoolId"] == {"Ref": "UserPoolId"}

    connection_modes = {
        mode["AuthType"] for mode in event_config["ConnectionAuthModes"]
    }
    assert "AMAZON_COGNITO_USER_POOLS" in connection_modes

    log_config = event_config["LogConfig"]
    assert "CloudWatchLogsRoleArn" in log_config
    assert log_config.get("LogLevel") == "INFO"


def test_appsync_pulse_namespace_has_subscribe_publish_handlers(
    pulse_api_template: dict[str, Any],
) -> None:
    """The pulse channel namespace exposes OnSubscribe/OnPublish handler code.

    Validates: AppSync Events configuration (namespace + handlers)
    """
    namespace = pulse_api_template["Resources"]["PulseRealtimeNamespace"]["Properties"]
    assert namespace["Name"] == "pulse"

    subscribe_modes = {
        mode["AuthType"] for mode in namespace["SubscribeAuthModes"]
    }
    publish_modes = {mode["AuthType"] for mode in namespace["PublishAuthModes"]}
    assert "AMAZON_COGNITO_USER_POOLS" in subscribe_modes
    assert "AMAZON_COGNITO_USER_POOLS" in publish_modes

    handlers = namespace["CodeHandlers"]
    assert "onSubscribe" in handlers
    assert "onPublish" in handlers


def test_appsync_events_api_has_waf_association(
    pulse_api_template: dict[str, Any],
) -> None:
    """A WAF web-ACL association is defined for the AppSync Events API.

    Validates: AppSync Events configuration (WAF association)
    """
    resources = pulse_api_template["Resources"]
    associations = [
        resource
        for resource in resources.values()
        if resource.get("Type") == "AWS::WAFv2::WebACLAssociation"
    ]
    assert associations, "expected a WAFv2 WebACLAssociation for the realtime API"

    realtime_associations = [
        assoc
        for assoc in associations
        if assoc["Properties"].get("ResourceArn")
        == {"GetAtt": "PulseRealtimeEventApi.ApiArn"}
    ]
    assert realtime_associations, "WAF association must target the AppSync Events API"
