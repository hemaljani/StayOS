"""Unit tests for the dataset_generator.writer module.

Tests the BatchWriter class (batch chunking, exponential backoff on
UnprocessedItems, success/failure counting) and the convert_floats_to_decimal
helper function.
"""

from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import boto3
import moto
import pytest
from botocore.exceptions import ClientError

from dataset_generator.writer import BatchWriter, convert_floats_to_decimal
from dataset_generator.config import MAX_RETRIES


# ---------------------------------------------------------------------------
# convert_floats_to_decimal tests
# ---------------------------------------------------------------------------


class TestConvertFloatsToDecimal:
    """Tests for the recursive float-to-Decimal conversion helper."""

    def test_converts_simple_float(self) -> None:
        """A standalone float becomes a Decimal via str intermediary."""
        result = convert_floats_to_decimal(3.14)
        assert result == Decimal("3.14")
        assert isinstance(result, Decimal)

    def test_converts_float_in_dict(self) -> None:
        """Float values nested in a dict are converted."""
        data: Dict[str, Any] = {"rate": 249.99, "name": "Suite"}
        result = convert_floats_to_decimal(data)
        assert result == {"rate": Decimal("249.99"), "name": "Suite"}
        assert isinstance(result["rate"], Decimal)

    def test_converts_float_in_nested_dict(self) -> None:
        """Deeply nested floats are converted recursively."""
        data: Dict[str, Any] = {"outer": {"inner": {"value": 0.1}}}
        result = convert_floats_to_decimal(data)
        assert result["outer"]["inner"]["value"] == Decimal("0.1")

    def test_converts_float_in_list(self) -> None:
        """Float values inside lists are converted."""
        data: List[Any] = [1.5, 2.5, "text", 3]
        result = convert_floats_to_decimal(data)
        assert result == [Decimal("1.5"), Decimal("2.5"), "text", 3]

    def test_preserves_non_float_types(self) -> None:
        """Strings, ints, bools, and None pass through unchanged."""
        data: Dict[str, Any] = {
            "name": "Chicago",
            "count": 42,
            "active": True,
            "notes": None,
        }
        result = convert_floats_to_decimal(data)
        assert result == data

    def test_handles_empty_structures(self) -> None:
        """Empty dict and list return unchanged."""
        assert convert_floats_to_decimal({}) == {}
        assert convert_floats_to_decimal([]) == []

    def test_precision_preserved(self) -> None:
        """Decimal conversion via str avoids float precision artifacts."""
        # 0.1 as float has precision issues; str("0.1") -> Decimal("0.1") is exact
        result = convert_floats_to_decimal(0.1)
        assert result == Decimal("0.1")
        assert str(result) == "0.1"


# ---------------------------------------------------------------------------
# BatchWriter tests
# ---------------------------------------------------------------------------


class TestBatchWriter:
    """Tests for the BatchWriter class batch writing and retry logic."""

    @moto.mock_aws
    def test_writes_items_successfully(self) -> None:
        """Items are written in batches and success count is reported."""
        # Create a real mocked DynamoDB table
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="test-table",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Patch the module-level _dynamodb resource to use moto's mock
        with patch("dataset_generator.writer._dynamodb", dynamodb):
            writer = BatchWriter(table_name="test-table")
            items = [{"pk": f"item-{i}", "value": 1.5} for i in range(10)]
            result = writer.write_items(items)

        assert result == {"success": 10, "failed": 0, "skipped": 0, "readback_fallback": 0}
        assert writer.success_count == 10
        assert writer.failure_count == 0

    @moto.mock_aws
    def test_writes_more_than_25_items_in_multiple_batches(self) -> None:
        """Items exceeding batch size are split into multiple batches."""
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="test-table",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        with patch("dataset_generator.writer._dynamodb", dynamodb):
            writer = BatchWriter(table_name="test-table")
            # 60 items = 3 batches (25 + 25 + 10)
            items = [{"pk": f"item-{i}", "data": "test"} for i in range(60)]
            result = writer.write_items(items)

        assert result == {"success": 60, "failed": 0, "skipped": 0, "readback_fallback": 0}

    @moto.mock_aws
    def test_writes_exactly_25_items_in_one_batch(self) -> None:
        """Exactly 25 items form a single batch."""
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="test-table",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        with patch("dataset_generator.writer._dynamodb", dynamodb):
            writer = BatchWriter(table_name="test-table")
            items = [{"pk": f"item-{i}"} for i in range(25)]
            result = writer.write_items(items)

        assert result == {"success": 25, "failed": 0, "skipped": 0, "readback_fallback": 0}

    def test_handles_client_error(self) -> None:
        """ClientError from batch_write_item counts items as failed."""
        mock_dynamodb = MagicMock()
        mock_client = MagicMock()
        mock_dynamodb.meta.client = mock_client

        # Simulate a ClientError on batch_write_item
        mock_client.batch_write_item.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "Table not found",
                }
            },
            operation_name="BatchWriteItem",
        )

        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="nonexistent-table")
            items = [{"pk": f"item-{i}"} for i in range(5)]
            result = writer.write_items(items)

        assert result == {"success": 0, "failed": 5, "skipped": 0, "readback_fallback": 0}
        assert writer.failure_count == 5

    @patch("dataset_generator.writer.time.sleep")
    def test_retries_unprocessed_items_with_backoff(
        self, mock_sleep: MagicMock
    ) -> None:
        """UnprocessedItems trigger exponential backoff retries."""
        mock_dynamodb = MagicMock()
        mock_client = MagicMock()
        mock_dynamodb.meta.client = mock_client

        # First call returns 2 UnprocessedItems, second call succeeds
        unprocessed_response = {
            "UnprocessedItems": {
                "test-table": [
                    {"PutRequest": {"Item": {"pk": {"S": "item-0"}}}},
                    {"PutRequest": {"Item": {"pk": {"S": "item-1"}}}},
                ]
            }
        }
        success_response: Dict[str, Any] = {"UnprocessedItems": {}}

        mock_client.batch_write_item.side_effect = [
            unprocessed_response,
            success_response,
        ]

        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="test-table")
            items = [{"pk": f"item-{i}"} for i in range(5)]
            result = writer.write_items(items)

        # 3 succeeded on first try, 2 succeeded on retry
        assert result["success"] == 5
        assert result["failed"] == 0
        # Backoff: 50ms * 2^1 = 100ms -> sleep(0.1)
        mock_sleep.assert_called_once_with(0.1)

    @patch("dataset_generator.writer.time.sleep")
    def test_max_retries_exhausted_counts_failures(
        self, mock_sleep: MagicMock
    ) -> None:
        """After MAX_RETRIES, remaining items are counted as failures."""
        mock_dynamodb = MagicMock()
        mock_client = MagicMock()
        mock_dynamodb.meta.client = mock_client

        # Always return unprocessed items (simulates persistent throttling)
        unprocessed_response = {
            "UnprocessedItems": {
                "test-table": [
                    {"PutRequest": {"Item": {"pk": {"S": "item-0"}}}},
                ]
            }
        }
        # 9 calls: 1 initial + 8 retries (MAX_RETRIES=8)
        mock_client.batch_write_item.return_value = unprocessed_response

        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="test-table")
            items = [{"pk": "item-0"}]
            result = writer.write_items(items)

        # After MAX_RETRIES exhausted, the 1 remaining item is counted as failed
        assert result["failed"] == 1
        # sleep called MAX_RETRIES - 1 times (first call counts as attempt 0,
        # then attempts 1 through 8 with sleep before each retry)
        assert mock_sleep.call_count == 7

    @patch("dataset_generator.writer.time.sleep")
    def test_backoff_caps_at_max(self, mock_sleep: MagicMock) -> None:
        """Backoff delay is capped at BACKOFF_MAX_MS (5000ms)."""
        mock_dynamodb = MagicMock()
        mock_client = MagicMock()
        mock_dynamodb.meta.client = mock_client

        # Return unprocessed items for all attempts
        unprocessed_response = {
            "UnprocessedItems": {
                "test-table": [
                    {"PutRequest": {"Item": {"pk": {"S": "item-0"}}}},
                ]
            }
        }
        mock_client.batch_write_item.return_value = unprocessed_response

        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="test-table")
            items = [{"pk": "item-0"}]
            writer.write_items(items)

        # Verify the backoff values:
        # attempt 1: min(50*2^1, 5000) = 100ms -> 0.1s
        # attempt 2: min(50*2^2, 5000) = 200ms -> 0.2s
        # attempt 3: min(50*2^3, 5000) = 400ms -> 0.4s
        # attempt 4: min(50*2^4, 5000) = 800ms -> 0.8s
        # attempt 5: min(50*2^5, 5000) = 1600ms -> 1.6s
        # attempt 6: min(50*2^6, 5000) = 3200ms -> 3.2s
        # attempt 7: min(50*2^7, 5000) = 5000ms -> 5.0s (capped)
        sleep_values = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_values[-1] == 5.0  # Last sleep hits the cap

    @moto.mock_aws
    def test_float_to_decimal_conversion_in_write(self) -> None:
        """Float values in items are converted to Decimal before writing."""
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="test-table",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        with patch("dataset_generator.writer._dynamodb", dynamodb):
            writer = BatchWriter(table_name="test-table")
            items = [{"pk": "item-1", "rate": 249.99, "occupancy": 0.87}]
            writer.write_items(items)

        # Verify the item was written with Decimal values
        response = table.get_item(Key={"pk": "item-1"})
        item = response["Item"]
        assert item["rate"] == Decimal("249.99")
        assert item["occupancy"] == Decimal("0.87")

    def test_empty_items_list(self) -> None:
        """An empty items list returns zero counts without errors."""
        mock_dynamodb = MagicMock()

        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="test-table")
            result = writer.write_items([])

        assert result == {"success": 0, "failed": 0, "skipped": 0, "readback_fallback": 0}
        # No batch_write_item calls should be made
        mock_dynamodb.meta.client.batch_write_item.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotent-upsert (put-if-changed) tests
# ---------------------------------------------------------------------------


class TestBatchWriterIdempotentUpsert:
    """Tests for BatchWriter's idempotent-upsert (put-if-changed) write mode.

    The roll-forward path (Requirements 2.3, 2.4) writes with idempotent=True:
    only new or changed items are written, unchanged items are skipped, and
    nothing is ever deleted. Re-running with the same reference date is a no-op.
    """

    @staticmethod
    def _make_table() -> Any:
        """Create a moto-backed table with a composite key for upsert tests."""
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="upsert-table",
            KeySchema=[
                {"AttributeName": "propertyId", "KeyType": "HASH"},
                {"AttributeName": "roomNumber", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "propertyId", "AttributeType": "S"},
                {"AttributeName": "roomNumber", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        return dynamodb

    @moto.mock_aws
    def test_first_upsert_writes_all_items(self) -> None:
        """On an empty table, every item is new so all are written."""
        dynamodb = self._make_table()
        items = [
            {"propertyId": "P1", "roomNumber": f"{i}", "status": "AVAILABLE"}
            for i in range(5)
        ]

        with patch("dataset_generator.writer._dynamodb", dynamodb):
            writer = BatchWriter(table_name="upsert-table")
            result = writer.write_items(items, idempotent=True)

        assert result == {"success": 5, "failed": 0, "skipped": 0, "readback_fallback": 0}

    @moto.mock_aws
    def test_reupsert_same_items_is_noop(self) -> None:
        """Re-upserting identical items skips them all (idempotent no-op)."""
        dynamodb = self._make_table()
        items = [
            {"propertyId": "P1", "roomNumber": f"{i}", "status": "AVAILABLE"}
            for i in range(5)
        ]

        with patch("dataset_generator.writer._dynamodb", dynamodb):
            first = BatchWriter(table_name="upsert-table")
            first.write_items(items, idempotent=True)

            second = BatchWriter(table_name="upsert-table")
            result = second.write_items(items, idempotent=True)

        # Nothing changed, so everything is skipped and nothing is written.
        assert result == {"success": 0, "failed": 0, "skipped": 5, "readback_fallback": 0}

    @moto.mock_aws
    def test_only_changed_items_are_written(self) -> None:
        """Only items whose attributes changed are written; the rest skipped."""
        dynamodb = self._make_table()
        items = [
            {"propertyId": "P1", "roomNumber": f"{i}", "status": "AVAILABLE"}
            for i in range(5)
        ]

        with patch("dataset_generator.writer._dynamodb", dynamodb):
            first = BatchWriter(table_name="upsert-table")
            first.write_items(items, idempotent=True)

            # Change a single item's status; leave the other 4 identical.
            changed = [dict(item) for item in items]
            changed[2]["status"] = "OCCUPIED"

            second = BatchWriter(table_name="upsert-table")
            result = second.write_items(changed, idempotent=True)

        assert result == {"success": 1, "failed": 0, "skipped": 4, "readback_fallback": 0}

    @moto.mock_aws
    def test_idempotent_upsert_never_deletes(self) -> None:
        """A smaller re-upsert never removes previously written items."""
        dynamodb = self._make_table()
        table = dynamodb.Table("upsert-table")
        items = [
            {"propertyId": "P1", "roomNumber": f"{i}", "status": "AVAILABLE"}
            for i in range(5)
        ]

        with patch("dataset_generator.writer._dynamodb", dynamodb):
            first = BatchWriter(table_name="upsert-table")
            first.write_items(items, idempotent=True)

            # Re-upsert only a subset - the others must remain in the table.
            second = BatchWriter(table_name="upsert-table")
            second.write_items(items[:2], idempotent=True)

        remaining = table.scan()["Items"]
        assert len(remaining) == 5



    @patch("dataset_generator.writer.time.sleep")
    def test_readback_failure_falls_back_to_write_and_counts(
        self, mock_sleep: MagicMock
    ) -> None:
        """A persistent read-back ClientError writes the items and counts fallback.

        Review finding CR-6: when the idempotent read-back (BatchGetItem) keeps
        failing, the items must still be written (fail open) BUT the degradation
        must be signalled distinctly via ``readback_fallback_count`` rather than
        silently masquerading as genuine changes.
        """
        mock_dynamodb = MagicMock()
        mock_client = MagicMock()
        mock_dynamodb.meta.client = mock_client
        # Table.key_schema drives _get_key_attributes.
        mock_dynamodb.Table.return_value.key_schema = [
            {"AttributeName": "pk", "KeyType": "HASH"},
        ]
        # Read-back always errors; the write path succeeds cleanly.
        mock_client.batch_get_item.side_effect = ClientError(
            error_response={
                "Error": {"Code": "ProvisionedThroughputExceededException",
                          "Message": "throttled"},
            },
            operation_name="BatchGetItem",
        )
        mock_client.batch_write_item.return_value = {"UnprocessedItems": {}}

        items = [{"pk": f"item-{i}"} for i in range(5)]
        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="throttled-table")
            result = writer.write_items(items, idempotent=True)

        # All 5 were written (fail open) and all 5 are counted as read-back
        # fallbacks, NOT as skipped (which would falsely imply idempotency held).
        assert result == {
            "success": 5,
            "failed": 0,
            "skipped": 0,
            "readback_fallback": 5,
        }
        assert writer.readback_fallback_count == 5
        # The read-back was retried up to MAX_RETRIES before giving up.
        assert mock_client.batch_get_item.call_count == MAX_RETRIES

    @patch("dataset_generator.writer.time.sleep")
    def test_readback_recovers_after_transient_error(
        self, mock_sleep: MagicMock
    ) -> None:
        """A transient read-back error recovers on retry with no fallback count."""
        mock_dynamodb = MagicMock()
        mock_client = MagicMock()
        mock_dynamodb.meta.client = mock_client
        mock_dynamodb.Table.return_value.key_schema = [
            {"AttributeName": "pk", "KeyType": "HASH"},
        ]
        transient = ClientError(
            error_response={
                "Error": {"Code": "ProvisionedThroughputExceededException",
                          "Message": "throttled"},
            },
            operation_name="BatchGetItem",
        )
        # Fail twice, then return one stored item identical to item-0 so it is
        # skipped; the remaining items are new and get written.
        stored_item0 = {"pk": "item-0"}
        mock_client.batch_get_item.side_effect = [
            transient,
            transient,
            {"Responses": {"recover-table": [stored_item0]}, "UnprocessedKeys": {}},
        ]
        mock_client.batch_write_item.return_value = {"UnprocessedItems": {}}

        items = [{"pk": f"item-{i}"} for i in range(3)]
        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="recover-table")
            result = writer.write_items(items, idempotent=True)

        # No fallback: the read-back eventually succeeded. item-0 matched and was
        # skipped; item-1 and item-2 were written.
        assert writer.readback_fallback_count == 0
        assert result["readback_fallback"] == 0
        assert result["skipped"] == 1
        assert result["success"] == 2
        assert mock_client.batch_get_item.call_count == 3



    @moto.mock_aws
    def test_idempotent_write_of_empty_list_is_a_noop(self) -> None:
        """Idempotent write of an empty list skips the read-back entirely."""
        dynamodb = self._make_table()
        with patch("dataset_generator.writer._dynamodb", dynamodb):
            writer = BatchWriter(table_name="upsert-table")
            result = writer.write_items([], idempotent=True)

        assert result == {
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "readback_fallback": 0,
        }

    @patch("dataset_generator.writer.time.sleep")
    def test_readback_unprocessed_keys_exhaust_counts_fallback(
        self, mock_sleep: MagicMock
    ) -> None:
        """Read-back keys that never clear (persistent UnprocessedKeys) fall back.

        Distinct from a ClientError: here BatchGetItem returns 200 but keeps
        reporting UnprocessedKeys. After MAX_RETRIES the still-unread keys are
        treated as changed (written) and counted as read-back fallbacks so the
        degradation is visible (review finding CR-6).
        """
        mock_dynamodb = MagicMock()
        mock_client = MagicMock()
        mock_dynamodb.meta.client = mock_client
        mock_dynamodb.Table.return_value.key_schema = [
            {"AttributeName": "pk", "KeyType": "HASH"},
        ]
        # Always echo the requested keys back as UnprocessedKeys, never clearing.
        def _always_unprocessed(RequestItems):  # noqa: N803 - boto3 kwarg name
            return {
                "Responses": {},
                "UnprocessedKeys": RequestItems,
            }

        mock_client.batch_get_item.side_effect = _always_unprocessed
        mock_client.batch_write_item.return_value = {"UnprocessedItems": {}}

        items = [{"pk": f"item-{i}"} for i in range(3)]
        with patch("dataset_generator.writer._dynamodb", mock_dynamodb):
            writer = BatchWriter(table_name="stuck-table")
            result = writer.write_items(items, idempotent=True)

        # All 3 keys never resolved -> counted as fallback and written.
        assert writer.readback_fallback_count == 3
        assert result["readback_fallback"] == 3
        assert result["success"] == 3
        assert result["skipped"] == 0
