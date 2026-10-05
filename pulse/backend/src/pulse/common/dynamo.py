"""DynamoDB helpers for PULSE built on the shared boto3 factory.

PULSE uses the DynamoDB *resource* interface throughout so components work with
native Python types instead of low-level ``AttributeValue`` dicts, and use the
``Key``/``Attr`` condition builders rather than hand-built expression strings.
This module provides cached ``Table`` accessors so a table object is created
once per name and reused across warm invocations.

Table *names* always come from configuration (environment variables via
``pulse.common.config``); they are never hardcoded here (PYQUALITY-06,
NAMING-02).
"""

from __future__ import annotations

from functools import cache
from typing import Any

from pulse.common.aws import get_resource


@cache
def get_table(table_name: str) -> Any:
    """Return a cached DynamoDB ``Table`` resource for a table name.

    The underlying DynamoDB resource is the shared, adaptively-retrying resource
    from ``pulse.common.aws``. The ``Table`` object is cached per name so it is
    created once and reused.

    Args:
        table_name: The physical DynamoDB table name (from configuration), e.g.
            the value of ``ALERTS_TABLE_NAME``.

    Returns:
        A boto3 DynamoDB ``Table`` resource bound to ``table_name``.
    """
    return get_resource("dynamodb").Table(table_name)


def get_dynamo_client() -> Any:
    """Return the DynamoDB resource's underlying auto-marshalling client.

    Multi-item operations that the ``Table`` resource does not expose -- most
    notably ``transact_write_items`` used by the Action Executor for its
    all-or-nothing write-back + RESOLVED transaction -- are issued through this
    client. Because it is the *resource's* client (``resource.meta.client``), it
    still auto-marshals native Python types to/from DynamoDB ``AttributeValue``
    dicts, so callers work with plain Python values (no manual
    ``TypeSerializer``).

    Returns:
        The shared, adaptively-retrying DynamoDB client with high-level type
        marshalling registered.
    """
    return get_resource("dynamodb").meta.client


__all__ = ["get_table", "get_dynamo_client"]
