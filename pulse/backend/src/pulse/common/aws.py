"""Shared boto3 client/resource factory for PULSE.

Per PYQUALITY-06, boto3 clients are created once and reused across warm Lambda
invocations for connection reuse, and configured with an explicit retry policy
rather than relying on legacy defaults. This module holds a single module-level
``botocore.config.Config`` (adaptive retry mode) and hands out cached clients
and resources so components never construct their own ad-hoc clients or create
clients inside loops.

Clients are thread-safe once created, so caching and sharing them is safe.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import boto3
from botocore.config import Config

# Module-level shared client configuration. Adaptive retry mode adds
# client-side rate limiting on top of standard retries, which suits the
# Stream-driven, bursty PULSE pipeline (Bedrock/DynamoDB throttling).
BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=5,
    read_timeout=30,
    max_pool_connections=50,
)


@cache
def get_client(service_name: str) -> Any:
    """Return a cached boto3 low-level client for a service.

    The client is created on first request and reused thereafter (one instance
    per service name), configured with the shared adaptive-retry ``BOTO_CONFIG``.

    Args:
        service_name: The AWS service name, e.g. ``"bedrock-runtime"`` or
            ``"scheduler"``.

    Returns:
        A configured, cached boto3 client for the requested service.
    """
    return boto3.client(service_name, config=BOTO_CONFIG)


@cache
def get_resource(service_name: str) -> Any:
    """Return a cached boto3 high-level resource for a service.

    Only some services expose a resource interface (DynamoDB, S3, SQS, SNS,
    etc.). For DynamoDB the resource interface is preferred because it
    auto-marshals native Python types.

    Args:
        service_name: The AWS service name, e.g. ``"dynamodb"``.

    Returns:
        A configured, cached boto3 resource for the requested service.
    """
    return boto3.resource(service_name, config=BOTO_CONFIG)


__all__ = ["BOTO_CONFIG", "get_client", "get_resource"]
