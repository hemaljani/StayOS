"""Regression coverage for one revenue source and explicit inventory semantics."""

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
import yaml
from moto import mock_aws

import historical_briefs
from data_puller import _format_kpis
from seed_data import GM_SEED_DATA


def revenue_record(property_id="ALOHA-CHI-001", record_date="2026-10-08"):
    """A fractional revenue record that must survive every presentation path."""
    return {
        "propertyId": property_id,
        "date": record_date,
        "occupancyPct": Decimal("90.5"),
        "adr": Decimal("250.75"),
        "revpar": Decimal("226.25"),
        "vsLastWeek": Decimal("-1.0"),
        "vsBudget": Decimal("7.5"),
        "vsYOY": Decimal("0"),
        "arrivals": 133,
        "departures": 135,
        "confirmedReservations": 342,
        "availableRooms": 368,
    }


def load_gateway_tools(monkeypatch, item):
    """Load the real Gateway handler with an isolated DynamoDB resource."""
    module_path = (
        Path(__file__).parents[2] / "functions" / "tools" / "lambda_function.py"
    )
    spec = importlib.util.spec_from_file_location("correctness_tools", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resource = MagicMock()
    resource.Table.return_value.get_item.return_value = {"Item": item}
    monkeypatch.setattr(module, "_dynamodb_resource", resource)
    return module


def test_brief_historical_and_gateway_use_identical_fractional_kpis(monkeypatch):
    """The same property-day record supplies all rates, counts, and deltas."""
    item = revenue_record()
    tools = load_gateway_tools(monkeypatch, item)
    current = _format_kpis(item, [], [], item["date"])
    historical = historical_briefs._build_historical_brief_record(
        GM_SEED_DATA[0],
        historical_briefs.PROPERTY_PROFILES[item["propertyId"]],
        item["date"],
        6,
        revenue=item,
    )
    occupancy = tools.get_occupancy(item["propertyId"], {"date": item["date"]})["data"]
    revenue = tools.get_revenue(item["propertyId"], {"start_date": item["date"]})[
        "data"
    ]
    for kpis in [current, historical["dailyKPIs"]]:
        assert kpis["occupancy"]["current"] == occupancy["occupancyPct"] == 90.5
        assert kpis["adr"]["current"] == revenue["adr"] == 250.75
        assert kpis["revPAR"]["current"] == revenue["revpar"] == 226.25
        assert kpis["occupancy"]["vsBudget"] == revenue["vsBudget"] == 7.5
        assert kpis["arrivals"]["total"] == occupancy["arrivalsTotal"] == 133
        assert kpis["departures"]["total"] == occupancy["departuresTotal"] == 135
    assert occupancy["totalRooms"] == 368
    assert "availableRooms" not in occupancy
    assert historical["dataSourceStatus"] == {"DATASET_REVENUES": "SUCCESS"}
    assert "90.5%" in historical["narrative"]
    assert "250.75" in historical["narrative"]
    assert "226.25" in historical["narrative"]


@pytest.mark.parametrize("item", [{}, revenue_record()])
def test_occupancy_tool_does_not_publish_remaining_room_field(monkeypatch, item):
    """No-data and legacy dataset records both avoid ambiguous availability."""
    tools = load_gateway_tools(monkeypatch, item)
    data = tools.get_occupancy("ALOHA-CHI-001", {"date": "2026-10-08"})["data"]
    assert data["totalRooms"] == (368 if item else 0)
    assert "availableRooms" not in data


def test_partial_revenue_retains_reservation_arrival_fallback():
    """Missing arrival totals retain the existing reservation-count fallback."""
    item = revenue_record()
    del item["arrivals"]
    kpis = _format_kpis(item, [{}, {}], [], item["date"])
    assert kpis["arrivals"]["total"] == 2
    assert "arrivals" not in item


def test_orchestrator_template_uses_operational_dataset():
    """Deployment cannot silently switch briefs back to independent samples."""

    class TemplateLoader(yaml.SafeLoader):
        pass

    TemplateLoader.add_multi_constructor(
        "!",
        lambda loader, _tag, node: (
            loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode)
            else loader.construct_sequence(node)
        ),
    )
    template = Path(__file__).parents[3] / "infrastructure/nested-stacks/compute.yaml"
    document = yaml.load(template.read_text(), Loader=TemplateLoader)
    variables = document["Resources"]["OrchestratorFunction"]["Properties"][
        "Environment"
    ]["Variables"]
    assert variables["MOCK_MODE"] == "false"
    assert variables["REVENUES_TABLE_NAME"] == "RevenuesTableName"


def test_historical_seed_reads_all_pages_and_skips_missing_source(monkeypatch):
    """Real DynamoDB marshaling/pagination preserves decimals and property scope."""
    today = datetime.now(timezone.utc).date().isoformat()
    with mock_aws():
        resource = boto3.resource("dynamodb")
        for table_name, sort_key in [
            ("test-revenues", "date"),
            ("test-briefs", "briefDate"),
        ]:
            resource.create_table(
                TableName=table_name,
                KeySchema=[
                    {"AttributeName": "propertyId", "KeyType": "HASH"},
                    {"AttributeName": sort_key, "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "propertyId", "AttributeType": "S"},
                    {"AttributeName": sort_key, "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        revenues = resource.Table("test-revenues")
        revenues.put_item(Item=revenue_record(record_date=today))
        # Another property's record must not supply the Chicago brief.
        revenues.put_item(Item=revenue_record("ALOHA-MIA-001", today))
        paginator = resource.meta.client.get_paginator("scan")
        pages_read = []

        class SmallPages:
            def paginate(self, **kwargs):
                for page in paginator.paginate(
                    **kwargs, PaginationConfig={"PageSize": 1}
                ):
                    pages_read.append(page)
                    yield page

        monkeypatch.setattr(
            resource.meta.client, "get_paginator", lambda _name: SmallPages()
        )
        monkeypatch.setattr(historical_briefs, "_dynamodb_resource", resource)
        count = historical_briefs.seed_historical_briefs(
            "test-briefs",
            [GM_SEED_DATA[0]],
            days=2,
            revenues_table_name="test-revenues",
        )
        assert len(pages_read) == 2
        assert count == 1  # yesterday has no source, so it is not fabricated
        records = resource.Table("test-briefs").scan()["Items"]
        assert len(records) == 1
        assert records[0]["briefDate"] == today
        assert records[0]["dailyKPIs"]["revPAR"]["current"] == Decimal("226.25")
        assert records[0]["dailyKPIs"]["occupancy"]["current"] == Decimal("90.5")


@pytest.mark.parametrize("already_seeded", [False, True])
def test_seed_handler_maps_revenues_after_creation_or_idempotent_skip(
    monkeypatch, already_seeded
):
    """Fresh installs and updates both seed briefs from persisted revenue."""
    today = datetime.now(timezone.utc).date().isoformat()
    with mock_aws():
        resource = boto3.resource("dynamodb")
        for table_name, sort_key in [
            ("stayos-revenues-test", "date"),
            ("stayos-briefs-test", "briefDate"),
        ]:
            resource.create_table(
                TableName=table_name,
                KeySchema=[
                    {"AttributeName": "propertyId", "KeyType": "HASH"},
                    {"AttributeName": sort_key, "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "propertyId", "AttributeType": "S"},
                    {"AttributeName": sort_key, "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )

        def write_revenue(_writer=None):
            resource.Table("stayos-revenues-test").put_item(
                Item=revenue_record(record_date=today)
            )
            return {}

        if already_seeded:
            write_revenue()
        path = (
            Path(__file__).parents[2] / "functions" / "seed-data" / "lambda_function.py"
        )
        spec = importlib.util.spec_from_file_location("correctness_seed", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(historical_briefs, "_dynamodb_resource", resource)
        monkeypatch.setattr(module, "GM_SEED_DATA", [GM_SEED_DATA[0]])
        monkeypatch.setattr(
            module, "_tables_already_seeded", lambda _names: already_seeded
        )
        for name in [
            "provision_cognito_users",
            "seed_settings_table",
            "provision_schedules",
        ]:
            monkeypatch.setattr(module, name, MagicMock(return_value=1))
        for name in [
            "BatchWriter",
            "generate_rooms",
            "generate_guests",
            "generate_reservations",
            "generate_work_orders",
            "reconcile_room_status",
        ]:
            monkeypatch.setattr(module, name, MagicMock())
        generate = MagicMock(side_effect=write_revenue)
        monkeypatch.setattr(module, "generate_revenue", generate)
        response = MagicMock()
        monkeypatch.setattr(module, "send_cfn_response", response)

        module.lambda_handler({"RequestType": "Create"}, MagicMock())

        assert generate.call_count == (0 if already_seeded else 1)
        assert response.call_args.kwargs["status"] == "SUCCESS"
        records = resource.Table("stayos-briefs-test").scan()["Items"]
        assert len(records) == 1
        assert records[0]["briefDate"] == today
        assert records[0]["dailyKPIs"]["revPAR"]["current"] == Decimal("226.25")
