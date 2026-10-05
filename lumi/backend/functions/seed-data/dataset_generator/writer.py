"""DynamoDB BatchWriteItem utility with exponential backoff for LUMI dataset seeding.

Provides the BatchWriter class that handles bulk loading of items into DynamoDB
tables using BatchWriteItem. Supports automatic chunking (25 items per batch),
exponential backoff on UnprocessedItems, and Decimal conversion for float values.

Used by all dataset generator modules (rooms, guests, revenue, reservations,
work orders) to write generated data to DynamoDB during the seed-data Lambda
custom resource execution.

Supports REQ-DS-8 (BatchWriteItem with 25 items/batch, exponential backoff).
"""

import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from dataset_generator.config import (
    BACKOFF_BASE_MS,
    BACKOFF_MAX_MS,
    BATCH_WRITE_SIZE,
    MAX_RETRIES,
)

logger = logging.getLogger(__name__)

# Module-level boto3 DynamoDB resource for connection reuse across invocations.
# Standard retry mode handles transient errors at the SDK level.
_dynamodb = boto3.resource(
    "dynamodb",
    config=Config(retries={"mode": "standard"}),
)


def convert_floats_to_decimal(obj: Any) -> Any:
    """Recursively convert float values to Decimal for DynamoDB compatibility.

    DynamoDB does not accept Python float types. This function walks a nested
    dict/list structure and converts any float values to Decimal using string
    intermediary to avoid floating-point precision artifacts.

    Args:
        obj: A value of any type — dict, list, float, or primitive. Nested
            structures are traversed recursively.

    Returns:
        The same structure with all float values replaced by Decimal equivalents.
        Non-float primitives (str, int, bool, None) are returned unchanged.
    """
    if isinstance(obj, dict):
        return {key: convert_floats_to_decimal(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [convert_floats_to_decimal(item) for item in obj]
    if isinstance(obj, float):
        # Use str() intermediary to preserve the decimal representation
        # and avoid float precision issues (e.g., 0.1 -> 0.1000000000000000055...)
        return Decimal(str(obj))
    return obj


class BatchWriter:
    """Writes items to a DynamoDB table in batches with retry logic.

    Splits items into chunks of 25 (DynamoDB BatchWriteItem limit), submits
    each batch, and retries UnprocessedItems with fixed exponential backoff.
    Tracks success and failure counts for reporting.

    Attributes:
        table_name: The DynamoDB table name to write to.
        success_count: Number of items successfully written.
        failure_count: Number of items that failed after max retries.
    """

    def __init__(self, table_name: str) -> None:
        """Initialize the BatchWriter for a specific DynamoDB table.

        Args:
            table_name: Name of the DynamoDB table to write items to.
                Must match an existing table accessible by the Lambda role.
        """
        self.table_name = table_name
        self.success_count: int = 0
        self.failure_count: int = 0
        # Number of items that were unchanged and therefore skipped in
        # idempotent-upsert mode (put-if-changed). Zero in plain write mode.
        self.skipped_count: int = 0
        # Number of items whose idempotent read-back could NOT be completed
        # (BatchGetItem errored after retries), so they were treated as changed
        # and rewritten - i.e. idempotent-upsert silently degraded toward a full
        # write for these items. Surfaced distinctly so a transient throttle is
        # not confused with genuine changes (review finding CR-6). Zero on the
        # healthy path and in plain write mode.
        self.readback_fallback_count: int = 0
        # Lazily resolved list of key attribute names for this table, used by
        # idempotent-upsert mode to build the key for each item's read-back.
        self._key_attributes: Optional[List[str]] = None

    def write_items(
        self, items: List[Dict[str, Any]], idempotent: bool = False
    ) -> Dict[str, int]:
        """Write a list of items to DynamoDB in batches of 25.

        Splits the input list into chunks of BATCH_WRITE_SIZE, converts float
        values to Decimal, and submits each chunk via batch_write_item. Any
        UnprocessedItems are retried with exponential backoff up to MAX_RETRIES.

        When ``idempotent`` is True, this uses the Idempotent_Upsert write mode
        required by the roll-forward path (Requirements 2.3, 2.4): each item is
        compared against the currently stored item and only written if it is
        new or its attributes changed (put-if-changed). This path NEVER deletes
        items, so re-running with the same reference date is a no-op. Batch
        chunking and exponential backoff are unchanged.

        Args:
            items: List of item dicts to write. Each dict represents a single
                DynamoDB item with attribute names as keys. Float values are
                automatically converted to Decimal before writing.
            idempotent: When True, only write new or changed items (put-if-changed)
                and never delete. When False (default), write every item.

        Returns:
            A dict with counts: {"success": N, "failed": N, "skipped": N,
            "readback_fallback": N}. "success" counts items written, "failed"
            counts items that failed after retries, "skipped" counts unchanged
            items skipped in idempotent mode (always 0 in plain write mode), and
            "readback_fallback" counts items written because their idempotent
            read-back could not be completed (0 on the healthy path).
        """
        items_to_write = items
        if idempotent:
            # Filter out items whose stored copy is byte-for-byte equivalent so
            # a re-run with the same reference date results in no net change.
            items_to_write = self._filter_changed_items(items)

        # Split items into chunks of 25 (BatchWriteItem limit)
        chunks = [
            items_to_write[i : i + BATCH_WRITE_SIZE]
            for i in range(0, len(items_to_write), BATCH_WRITE_SIZE)
        ]

        for chunk_index, chunk in enumerate(chunks):
            self._write_batch(chunk, chunk_index)

        logger.info(
            "Completed writing to %s: %d succeeded, %d failed, %d skipped, "
            "%d read-back-fallback (idempotent=%s)",
            self.table_name,
            self.success_count,
            self.failure_count,
            self.skipped_count,
            self.readback_fallback_count,
            idempotent,
        )

        return {
            "success": self.success_count,
            "failed": self.failure_count,
            "skipped": self.skipped_count,
            "readback_fallback": self.readback_fallback_count,
        }

    def _get_key_attributes(self) -> List[str]:
        """Resolve and cache the table's key attribute names.

        Reads the table's KeySchema once (partition key plus optional sort key)
        so idempotent-upsert mode can build each item's primary key for the
        read-back comparison.

        Returns:
            Ordered list of key attribute names (partition key first, then the
            sort key if the table has one).
        """
        if self._key_attributes is None:
            table = _dynamodb.Table(self.table_name)
            self._key_attributes = [
                key["AttributeName"] for key in table.key_schema
            ]
        return self._key_attributes

    def _filter_changed_items(
        self, items: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Return only items that are new or changed vs their stored copy.

        Implements put-if-changed for the Idempotent_Upsert write mode. Reads
        each item's current stored version via BatchGetItem (in chunks) and
        compares it (after float->Decimal conversion, matching what would be
        written) against the incoming item. Unchanged items are counted as
        skipped and dropped; new or changed items are returned for writing.
        This path never deletes.

        Args:
            items: The full list of candidate item dicts to upsert.

        Returns:
            The subset of items that are new or differ from their stored copy.
        """
        if not items:
            return []

        key_attributes = self._get_key_attributes()
        # Convert up front so the comparison matches exactly what would be stored.
        converted_items = [convert_floats_to_decimal(item) for item in items]

        existing_by_key = self._batch_get_existing(converted_items, key_attributes)

        changed_items: List[Dict[str, Any]] = []
        for item in converted_items:
            key_tuple = self._item_key_tuple(item, key_attributes)
            stored = existing_by_key.get(key_tuple)
            if stored is not None and stored == item:
                # Stored copy is identical - skip to keep the re-run a no-op.
                self.skipped_count += 1
            else:
                changed_items.append(item)

        return changed_items

    def _item_key_tuple(
        self, item: Dict[str, Any], key_attributes: List[str]
    ) -> Tuple[Any, ...]:
        """Build a hashable primary-key tuple for an item.

        Args:
            item: A DynamoDB item dict.
            key_attributes: Ordered key attribute names for the table.

        Returns:
            Tuple of the item's key attribute values, usable as a dict key.
        """
        return tuple(item.get(attr) for attr in key_attributes)

    def _batch_get_existing(
        self, items: List[Dict[str, Any]], key_attributes: List[str]
    ) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
        """Read the currently stored version of each item via BatchGetItem.

        Chunks the keys into BATCH_WRITE_SIZE groups (the BatchGetItem limit is
        100, but reusing the batch size keeps requests small and consistent),
        handles UnprocessedKeys with exponential backoff, and returns a lookup
        from key tuple to stored item. Missing items simply have no entry.

        Args:
            items: The (already converted) items whose stored versions to fetch.
            key_attributes: Ordered key attribute names for the table.

        Returns:
            Dict mapping each item's key tuple to its stored item dict. Items
            that do not yet exist are absent from the map.
        """
        existing_by_key: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

        key_chunks = [
            items[i : i + BATCH_WRITE_SIZE]
            for i in range(0, len(items), BATCH_WRITE_SIZE)
        ]

        for chunk in key_chunks:
            keys = [
                {attr: item[attr] for attr in key_attributes} for item in chunk
            ]
            request_items: Dict[str, Any] = {self.table_name: {"Keys": keys}}
            attempt = 0

            while request_items:
                try:
                    response = _dynamodb.meta.client.batch_get_item(
                        RequestItems=request_items
                    )
                except ClientError as error:
                    error_code = error.response["Error"]["Code"]
                    # Retry the read-back a bounded number of times before
                    # falling back. A transient throttle/service blip should not
                    # silently convert idempotent-upsert into a full rewrite
                    # (review finding CR-6); only give up after MAX_RETRIES.
                    attempt += 1
                    if attempt < MAX_RETRIES:
                        delay_ms = min(
                            BACKOFF_BASE_MS * (2 ** attempt), BACKOFF_MAX_MS
                        )
                        logger.warning(
                            "DynamoDB batch_get_item error for table %s during "
                            "idempotent read-back (attempt %d/%d): %s - %s; "
                            "retrying in %dms",
                            self.table_name,
                            attempt,
                            MAX_RETRIES,
                            error_code,
                            error.response["Error"]["Message"],
                            delay_ms,
                        )
                        time.sleep(delay_ms / 1000.0)
                        continue
                    # Retries exhausted: fail open (treat the unread keys as
                    # changed so they get written) but record the degradation
                    # distinctly so it is not mistaken for genuine changes.
                    unread_count = sum(
                        len(entry["Keys"]) for entry in request_items.values()
                    )
                    self.readback_fallback_count += unread_count
                    logger.warning(
                        "DynamoDB batch_get_item failed for table %s after %d "
                        "attempts (%s: %s); idempotent read-back degraded to a "
                        "full write for %d item(s) in this batch.",
                        self.table_name,
                        attempt,
                        error_code,
                        error.response["Error"]["Message"],
                        unread_count,
                    )
                    break

                for stored in response.get("Responses", {}).get(
                    self.table_name, []
                ):
                    key_tuple = self._item_key_tuple(stored, key_attributes)
                    existing_by_key[key_tuple] = stored

                unprocessed = response.get("UnprocessedKeys", {})
                if not unprocessed:
                    break

                attempt += 1
                if attempt >= MAX_RETRIES:
                    unread_count = sum(
                        len(entry["Keys"]) for entry in unprocessed.values()
                    )
                    self.readback_fallback_count += unread_count
                    logger.warning(
                        "Max retries reached reading existing items from %s; "
                        "%d remaining key(s) treated as changed (idempotent "
                        "read-back degraded to a full write for them).",
                        self.table_name,
                        unread_count,
                    )
                    break

                delay_ms = min(BACKOFF_BASE_MS * (2 ** attempt), BACKOFF_MAX_MS)
                time.sleep(delay_ms / 1000.0)
                request_items = unprocessed

        return existing_by_key

    def _write_batch(self, chunk: List[Dict[str, Any]], chunk_index: int) -> None:
        """Write a single batch of up to 25 items with retry on UnprocessedItems.

        Submits the batch to DynamoDB and retries any UnprocessedItems using
        fixed exponential backoff (no jitter) up to MAX_RETRIES attempts.

        Args:
            chunk: A list of up to 25 item dicts to write in one batch request.
            chunk_index: Zero-based index of this chunk within the overall
                write operation, used for logging context.

        Raises:
            ClientError: If a non-retryable DynamoDB error occurs (e.g.,
                ValidationException, ResourceNotFoundException).
        """
        # Convert floats to Decimal for DynamoDB compatibility
        converted_chunk = [convert_floats_to_decimal(item) for item in chunk]

        # Build the PutRequest structure for batch_write_item
        request_items: Dict[str, List[Dict[str, Any]]] = {
            self.table_name: [
                {"PutRequest": {"Item": item}} for item in converted_chunk
            ]
        }

        attempt = 0
        items_in_batch = len(converted_chunk)

        while request_items:
            try:
                # Submit the batch to DynamoDB
                response = _dynamodb.meta.client.batch_write_item(
                    RequestItems=request_items
                )
            except ClientError as error:
                error_code = error.response["Error"]["Code"]
                logger.error(
                    "DynamoDB batch_write_item failed for table %s, "
                    "chunk %d, attempt %d: %s - %s",
                    self.table_name,
                    chunk_index,
                    attempt,
                    error_code,
                    error.response["Error"]["Message"],
                )
                # Count all remaining items as failed
                remaining = sum(
                    len(puts) for puts in request_items.values()
                )
                self.failure_count += remaining
                return

            # Check for UnprocessedItems that need retry
            unprocessed = response.get("UnprocessedItems", {})

            if not unprocessed:
                # All items in this batch succeeded
                self.success_count += items_in_batch
                logger.info(
                    "Batch %d written to %s: %d items",
                    chunk_index,
                    self.table_name,
                    items_in_batch,
                )
                return

            # Some items were not processed — prepare retry
            attempt += 1
            unprocessed_count = sum(
                len(puts) for puts in unprocessed.values()
            )
            processed_this_round = items_in_batch - unprocessed_count
            self.success_count += processed_this_round
            # Update items_in_batch for the next iteration to track only remaining
            items_in_batch = unprocessed_count

            if attempt >= MAX_RETRIES:
                logger.error(
                    "Max retries (%d) exhausted for table %s, chunk %d. "
                    "%d items remain unprocessed.",
                    MAX_RETRIES,
                    self.table_name,
                    chunk_index,
                    unprocessed_count,
                )
                self.failure_count += unprocessed_count
                return

            # Fixed exponential backoff: min(base * 2^attempt, max)
            delay_ms = min(
                BACKOFF_BASE_MS * (2 ** attempt),
                BACKOFF_MAX_MS,
            )

            logger.warning(
                "Retrying %d UnprocessedItems for table %s, chunk %d "
                "(attempt %d/%d, backoff %dms)",
                unprocessed_count,
                self.table_name,
                chunk_index,
                attempt,
                MAX_RETRIES,
                delay_ms,
            )

            time.sleep(delay_ms / 1000.0)
            request_items = unprocessed
