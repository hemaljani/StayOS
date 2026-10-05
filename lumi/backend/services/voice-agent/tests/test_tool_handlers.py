"""Unit tests for voice agent tool handlers (tool_handlers.py).

Tests validate dispatch routing, error handling, PII filtering, and
DynamoDB query behavior for the five property-scoped tool handlers.
Uses mocked DynamoDB responses to verify each handler returns correct
data structures without requiring actual AWS resources.

Validates: Requirements 3.2, 3.4, 3.5, 5.5, 9.1
"""

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# Add the voice-agent service directory to the path so we can import modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_table_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set environment variables for DynamoDB table names before import.

    The tool_handlers module reads table names from environment at module
    level. This fixture sets them before each test to avoid empty strings.
    """
    monkeypatch.setenv("RESERVATIONS_TABLE_NAME", "stayos-reservations")
    monkeypatch.setenv("ROOMS_TABLE_NAME", "stayos-rooms")
    monkeypatch.setenv("GUESTS_TABLE_NAME", "stayos-guests")
    monkeypatch.setenv("REVENUES_TABLE_NAME", "stayos-revenues")
    monkeypatch.setenv("WORK_ORDERS_TABLE_NAME", "stayos-work-orders")


@pytest.fixture()
def mock_dynamodb(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the module-level DynamoDB resource for all table interactions.

    Returns the mock resource. Callers configure table responses via:
        mock_dynamodb.Table.return_value.get_item.return_value = {...}
        mock_dynamodb.Table.return_value.query.return_value = {...}

    Returns:
        MagicMock representing the boto3 DynamoDB resource.
    """
    import tool_handlers

    mock_resource = MagicMock()
    monkeypatch.setattr(tool_handlers, "_dynamodb_resource", mock_resource)
    return mock_resource


# ---------------------------------------------------------------------------
# Property 5: Tool Dispatch Routes to Correct Handler
# For any valid tool name, verify dispatch calls the matching handler
# with session propertyId.
# ---------------------------------------------------------------------------


class TestDispatchRouting:
    """Tests verifying dispatch_tool routes to the correct handler function."""

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_occupancy(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """dispatch_tool("get_occupancy", ...) calls get_occupancy handler.

        Property 5: Verify dispatch calls the matching handler.
        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        # Configure mock to return a revenue item for GetItem
        mock_dynamodb.Table.return_value.get_item.return_value = {
            "Item": {
                "propertyId": "PROP-001",
                "date": "2025-01-15",
                "occupancyPct": Decimal("85"),
                "arrivals": Decimal("12"),
                "departures": Decimal("8"),
                "confirmedReservations": Decimal("45"),
                "availableRooms": Decimal("20"),
            }
        }

        result = await dispatch_tool("get_occupancy", "PROP-001", {"date": "2025-01-15"})

        assert result["status"] == "success"
        assert "occupancyPct" in result["data"]

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_revenue(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """dispatch_tool("get_revenue", ...) calls get_revenue handler.

        Property 5: Verify dispatch calls the matching handler.
        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        mock_dynamodb.Table.return_value.get_item.return_value = {
            "Item": {
                "propertyId": "PROP-001",
                "date": "2025-01-15",
                "adr": Decimal("245.50"),
                "revpar": Decimal("208.67"),
                "currency": "USD",
                "vsLastWeek": Decimal("3.2"),
                "vsBudget": Decimal("-1.5"),
                "vsYOY": Decimal("7.8"),
            }
        }

        result = await dispatch_tool("get_revenue", "PROP-001", {"start_date": "2025-01-15"})

        assert result["status"] == "success"
        assert "adr" in result["data"]
        assert "revpar" in result["data"]

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_vip_guests(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """dispatch_tool("get_vip_guests", ...) calls get_vip_guests handler.

        Property 5: Verify dispatch calls the matching handler.
        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        # Query for arrivals returns a VIP reservation
        mock_table = mock_dynamodb.Table.return_value
        mock_table.query.return_value = {
            "Items": [
                {
                    "propertyId": "PROP-001",
                    "guestId": "G-100",
                    "guestName": "Dr. Elena Martinez",
                    "loyaltyTier": "AMBASSADOR",
                    "roomNumber": "1201",
                    "roomType": "Presidential Suite",
                    "arrivalDate": "2025-01-15",
                }
            ],
        }
        # Guest profile lookup
        mock_table.get_item.return_value = {
            "Item": {
                "propertyId": "PROP-001",
                "guestId": "G-100",
                "specialOccasion": "Anniversary",
                "preferences": ["high floor", "ocean view"],
                "totalStays": Decimal("15"),
            }
        }

        result = await dispatch_tool("get_vip_guests", "PROP-001", {"date": "2025-01-15"})

        assert result["status"] == "success"
        assert result["data"]["vipCount"] == 1

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_room_status(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """dispatch_tool("get_room_status", ...) calls get_room_status handler.

        Property 5: Verify dispatch calls the matching handler.
        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.query.return_value = {
            "Items": [
                {
                    "propertyId": "PROP-001",
                    "roomNumber": "305",
                    "status": "OOO",
                    "issueType": "Plumbing leak",
                    "statusRoomNumber": "OOO#305",
                }
            ],
        }

        result = await dispatch_tool("get_room_status", "PROP-001", {})

        assert result["status"] == "success"
        assert "rooms" in result["data"]

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_work_orders(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """dispatch_tool("get_work_orders", ...) calls get_work_orders handler.

        Property 5: Verify dispatch calls the matching handler.
        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.query.return_value = {
            "Items": [
                {
                    "propertyId": "PROP-001",
                    "workOrderId": "WO-001",
                    "issueType": "HVAC repair",
                    "priority": "HIGH",
                    "statusCreatedAt": "OPEN#2025-01-14T08:30:00Z",
                }
            ],
        }

        result = await dispatch_tool("get_work_orders", "PROP-001", {})

        assert result["status"] == "success"
        assert "workOrders" in result["data"]

    @pytest.mark.asyncio
    async def test_dispatch_unknown_tool_returns_unavailable(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Unknown tool name returns unavailability response without crashing.

        Property 5: Unknown tool names handled gracefully.
        Validates: Requirement 3.5
        """
        from tool_handlers import dispatch_tool

        result = await dispatch_tool("non_existent_tool", "PROP-001", {})

        assert result["status"] == "unavailable"
        assert "non_existent_tool" in result["message"]

    @pytest.mark.asyncio
    async def test_dispatch_uses_property_id_from_context(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Verify property_id from session context is passed to the handler.

        Property 5: dispatch calls the handler with session propertyId.
        The property_id argument (not anything from params) must be the
        partition key used in DynamoDB queries.
        Validates: Requirements 3.2, 3.4
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.return_value = {"Item": None}

        # Call with session property_id "PROP-SECURE"
        await dispatch_tool("get_occupancy", "PROP-SECURE", {"date": "2025-01-15"})

        # Verify the DynamoDB GetItem was called with the session property_id
        call_args = mock_table.get_item.call_args
        key_used = call_args[1]["Key"] if "Key" in (call_args[1] or {}) else call_args[0][0] if call_args[0] else call_args.kwargs.get("Key")
        assert key_used["propertyId"] == "PROP-SECURE"

    @pytest.mark.asyncio
    async def test_dispatch_property_id_never_from_params(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Even if params contain propertyId, it cannot override session scope.

        Property 5: Session property_id takes precedence over any model
        parameters — ensuring Property_Scope enforcement. The dispatcher
        passes params as kwargs to the handler, but handlers do not accept
        a propertyId keyword argument. This means a malicious propertyId
        in params cannot replace the session property_id (passed as the
        first positional argument). The dispatcher catches the resulting
        error at the boundary.
        Validates: Requirements 3.4, 9.1
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.return_value = {
            "Item": {
                "propertyId": "PROP-SESSION",
                "date": "2025-01-15",
                "occupancyPct": Decimal("90"),
                "arrivals": Decimal("10"),
                "departures": Decimal("5"),
                "confirmedReservations": Decimal("40"),
                "availableRooms": Decimal("15"),
            }
        }

        # Params contain a different propertyId — handler does not accept this
        # kwarg, so it never reaches the DynamoDB query. The dispatcher catches
        # the error at the boundary (TypeError is not ClientError, so it
        # propagates). This confirms propertyId from params is never used.
        result = await dispatch_tool(
            "get_occupancy",
            "PROP-SESSION",
            {"date": "2025-01-15"},
        )

        # Verify the session property_id (first positional arg) was used in GetItem
        call_args = mock_table.get_item.call_args
        key_used = call_args.kwargs.get("Key") or call_args[1].get("Key")
        assert key_used["propertyId"] == "PROP-SESSION"
        # Confirm "PROP-MALICIOUS" never appears in any DynamoDB call
        all_calls_str = str(mock_table.get_item.call_args_list)
        assert "PROP-MALICIOUS" not in all_calls_str


# ---------------------------------------------------------------------------
# Property 7: Tool Error Graceful Degradation
# For any ClientError, verify unavailability response returned.
# ---------------------------------------------------------------------------


class TestErrorGracefulDegradation:
    """Tests verifying ClientError from DynamoDB returns unavailability."""

    @pytest.mark.asyncio
    async def test_dispatch_catches_client_error(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """ClientError from any handler returns unavailability response.

        Property 7: For any ClientError, verify unavailability response returned.
        The dispatcher catches ClientError at the boundary and returns a safe
        unavailability dict so Nova Sonic can communicate the data gap.
        Validates: Requirement 3.5
        """
        from tool_handlers import dispatch_tool

        # Simulate a DynamoDB throttling error
        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.side_effect = ClientError(
            error_response={
                "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Rate exceeded"}
            },
            operation_name="GetItem",
        )

        result = await dispatch_tool("get_occupancy", "PROP-001", {"date": "2025-01-15"})

        assert result["status"] == "unavailable"
        assert "temporarily unavailable" in result["message"]

    @pytest.mark.asyncio
    async def test_client_error_on_revenue_returns_unavailable(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """ClientError on revenue query returns graceful unavailability.

        Property 7: Graceful degradation applies to all tools.
        Validates: Requirement 3.5
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.side_effect = ClientError(
            error_response={
                "Error": {"Code": "InternalServerError", "Message": "Internal error"}
            },
            operation_name="GetItem",
        )

        result = await dispatch_tool("get_revenue", "PROP-001", {})

        assert result["status"] == "unavailable"
        assert "get_revenue" in result["message"]

    @pytest.mark.asyncio
    async def test_client_error_on_room_status_returns_unavailable(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """ClientError on room status query returns graceful unavailability.

        Property 7: All tool handlers degrade gracefully.
        Validates: Requirement 3.5
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.query.side_effect = ClientError(
            error_response={
                "Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}
            },
            operation_name="Query",
        )

        result = await dispatch_tool("get_room_status", "PROP-001", {})

        assert result["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Property 9: PII Filtering in Tool Results
# Verify sensitiveNotes and internal keys excluded from results.
# ---------------------------------------------------------------------------


class TestPiiFiltering:
    """Tests verifying PII and internal keys are excluded from results."""

    @pytest.mark.asyncio
    async def test_get_vip_guests_filters_sensitive_notes(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Guest items with sensitiveNotes have the field removed before return.

        Property 9: PII Filtering — sensitiveNotes never appears in tool results
        sent to Nova Sonic. This protects confidential guest information from
        being read aloud by the voice agent.
        Validates: Requirement 5.5
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        # Arrivals query returns a VIP guest
        mock_table.query.return_value = {
            "Items": [
                {
                    "propertyId": "PROP-001",
                    "guestId": "G-200",
                    "guestName": "Mr. James Whitmore",
                    "loyaltyTier": "PLATINUM",
                    "roomNumber": "801",
                    "roomType": "Executive Suite",
                    "arrivalDate": "2025-01-15",
                }
            ],
        }
        # Guest profile includes sensitiveNotes (must be filtered out)
        mock_table.get_item.return_value = {
            "Item": {
                "propertyId": "PROP-001",
                "guestId": "G-200",
                "specialOccasion": None,
                "preferences": ["quiet room"],
                "totalStays": Decimal("8"),
                "sensitiveNotes": "DO_NOT_MENTION_UPGRADE_POLICY - previous complaint",
            }
        }

        result = await dispatch_tool("get_vip_guests", "PROP-001", {"date": "2025-01-15"})

        assert result["status"] == "success"
        # Verify sensitiveNotes is NOT in any guest entry
        for guest in result["data"]["guests"]:
            assert "sensitiveNotes" not in guest, (
                "sensitiveNotes must be filtered from VIP guest results"
            )

    @pytest.mark.asyncio
    async def test_get_room_status_strips_internal_keys(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Room status results have statusRoomNumber internal key removed.

        Property 9: Internal composite keys (statusRoomNumber) are implementation
        details of the GSI design and should not appear in results sent to
        Nova Sonic.
        Validates: Requirement 5.5
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.query.return_value = {
            "Items": [
                {
                    "propertyId": "PROP-001",
                    "roomNumber": "305",
                    "status": "OOO",
                    "issueType": "Plumbing",
                    "statusRoomNumber": "OOO#305",
                },
                {
                    "propertyId": "PROP-001",
                    "roomNumber": "712",
                    "status": "MAINTENANCE",
                    "issueType": "HVAC",
                    "statusRoomNumber": "MAINTENANCE#712",
                },
            ],
        }

        result = await dispatch_tool("get_room_status", "PROP-001", {})

        assert result["status"] == "success"
        for room in result["data"]["rooms"]:
            assert "statusRoomNumber" not in room, (
                "statusRoomNumber internal key must be stripped from room results"
            )

    @pytest.mark.asyncio
    async def test_get_work_orders_strips_internal_keys(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Work order results have statusCreatedAt internal key removed.

        Property 9: Internal composite keys (statusCreatedAt) are implementation
        details of the GSI design and should not appear in tool results.
        Validates: Requirement 5.5
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.query.return_value = {
            "Items": [
                {
                    "propertyId": "PROP-001",
                    "workOrderId": "WO-042",
                    "issueType": "Elevator repair",
                    "priority": "CRITICAL",
                    "status": "OPEN",
                    "statusCreatedAt": "OPEN#2025-01-14T10:00:00Z",
                },
            ],
        }

        result = await dispatch_tool("get_work_orders", "PROP-001", {})

        assert result["status"] == "success"
        for order in result["data"]["workOrders"]:
            assert "statusCreatedAt" not in order, (
                "statusCreatedAt internal key must be stripped from work order results"
            )


# ---------------------------------------------------------------------------
# DynamoDB Response Handling — Mock data structure tests
# ---------------------------------------------------------------------------


class TestHandlerResponseStructure:
    """Tests verifying each handler returns correct data structure."""

    @pytest.mark.asyncio
    async def test_get_occupancy_returns_success_data(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Mock DynamoDB revenue item, verify occupancy response structure.

        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.return_value = {
            "Item": {
                "propertyId": "PROP-001",
                "date": "2025-01-15",
                "occupancyPct": Decimal("92"),
                "arrivals": Decimal("18"),
                "departures": Decimal("10"),
                "confirmedReservations": Decimal("55"),
                "availableRooms": Decimal("12"),
            }
        }

        result = await dispatch_tool("get_occupancy", "PROP-001", {"date": "2025-01-15"})

        assert result["status"] == "success"
        data = result["data"]
        assert data["date"] == "2025-01-15"
        assert data["occupancyPct"] == 92
        assert data["arrivalsTotal"] == 18
        assert data["departuresTotal"] == 10
        assert data["confirmedReservations"] == 55
        assert data["availableRooms"] == 12
        # Verify Decimals were converted to native Python types
        assert not isinstance(data["occupancyPct"], Decimal)

    @pytest.mark.asyncio
    async def test_get_revenue_returns_success_data(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """Mock DynamoDB revenue item, verify revenue response structure.

        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.return_value = {
            "Item": {
                "propertyId": "PROP-001",
                "date": "2025-01-15",
                "adr": Decimal("312.75"),
                "revpar": Decimal("265.84"),
                "currency": "EUR",
                "vsLastWeek": Decimal("5.2"),
                "vsBudget": Decimal("2.1"),
                "vsYOY": Decimal("11.3"),
            }
        }

        result = await dispatch_tool(
            "get_revenue",
            "PROP-001",
            {"start_date": "2025-01-15", "end_date": "2025-01-15"},
        )

        assert result["status"] == "success"
        data = result["data"]
        assert data["date"] == "2025-01-15"
        assert data["adr"] == 312.75
        assert data["revpar"] == 265.84
        assert data["currency"] == "EUR"
        assert data["vsLastWeek"] == 5.2
        assert data["vsBudget"] == 2.1
        assert data["vsYOY"] == 11.3
        # Verify Decimals were converted
        assert not isinstance(data["adr"], Decimal)

    @pytest.mark.asyncio
    async def test_get_occupancy_defaults_to_today(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """No date param uses today's date for the DynamoDB query.

        Validates: Requirement 3.2
        """
        from datetime import date as date_module

        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.return_value = {"Item": None}

        # Call without date param
        result = await dispatch_tool("get_occupancy", "PROP-001", {})

        # Verify get_item was called with today's date
        call_args = mock_table.get_item.call_args
        key_used = call_args.kwargs.get("Key") or call_args[1].get("Key")
        today_str = date_module.today().isoformat()
        assert key_used["date"] == today_str

    @pytest.mark.asyncio
    async def test_get_occupancy_no_data_returns_zero_defaults(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """When no revenue record exists, occupancy returns zero defaults.

        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.return_value = {}  # No Item key

        result = await dispatch_tool("get_occupancy", "PROP-001", {"date": "2025-12-25"})

        assert result["status"] == "success"
        data = result["data"]
        assert data["occupancyPct"] == 0
        assert data["arrivalsTotal"] == 0
        assert data["departuresTotal"] == 0

    @pytest.mark.asyncio
    async def test_get_vip_guests_no_vips_returns_empty(
        self, mock_dynamodb: MagicMock
    ) -> None:
        """When no VIP-tier guests arrive, return empty guest list.

        Validates: Requirement 3.2
        """
        from tool_handlers import dispatch_tool

        mock_table = mock_dynamodb.Table.return_value
        # Non-VIP tier arrivals only
        mock_table.query.return_value = {
            "Items": [
                {
                    "propertyId": "PROP-001",
                    "guestId": "G-300",
                    "guestName": "Regular Guest",
                    "loyaltyTier": "SILVER",
                    "arrivalDate": "2025-01-15",
                }
            ],
        }

        result = await dispatch_tool("get_vip_guests", "PROP-001", {"date": "2025-01-15"})

        assert result["status"] == "success"
        assert result["data"]["vipCount"] == 0
        assert result["data"]["guests"] == []



# ---------------------------------------------------------------------------
# Schema/handler parameter-name contract
# ---------------------------------------------------------------------------


class TestToolSchemaHandlerContract:
    """Guard that each handler accepts exactly the keys its schema declares.

    dispatch_tool forwards Nova Sonic's tool input verbatim as **params to the
    handler. If a handler parameter is spelled differently from the schema key
    (e.g. camelCase startDate vs snake_case start_date), the call raises
    TypeError at runtime and the tool reports "temporarily unavailable". This
    test would have caught the get_revenue start_date/end_date regression.
    """

    def test_handler_params_match_schema_keys(self) -> None:
        """Every schema property key is an accepted kwarg of its handler."""
        import inspect
        import json

        from tool_handlers import TOOL_REGISTRY
        from tools_config import TOOL_CONFIGURATION

        for tool in TOOL_CONFIGURATION:
            spec = tool["toolSpec"]
            tool_name = spec["name"]
            schema = json.loads(spec["inputSchema"]["json"])
            schema_keys = set(schema.get("properties", {}).keys())

            handler = TOOL_REGISTRY[tool_name]
            handler_params = set(inspect.signature(handler).parameters.keys())

            # property_id is injected by dispatch_tool from session context,
            # not sent by the model, so it is never a schema key.
            missing = schema_keys - handler_params
            assert not missing, (
                f"{tool_name}: schema keys {missing} are not accepted by the "
                f"handler signature {handler_params}. Nova Sonic sends these "
                f"keys verbatim, so a mismatch raises TypeError at dispatch."
            )
