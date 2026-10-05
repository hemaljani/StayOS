"""Structured-logging factory for PULSE components.

PULSE uses AWS Lambda Powertools for structured JSON logging across every
Lambda (PYQUALITY-03: no ``print``). This module provides a single factory so
each component initializes its logger consistently with a service name and a
log level driven by the ``LOG_LEVEL`` environment variable.

Usage (at module level in a component, before the handler):

    from pulse.common.logging import get_logger

    logger = get_logger("pulse-rule-evaluator")
    logger.info("evaluating stream batch", extra={"recordCount": 12})
"""

from __future__ import annotations

import os

from aws_lambda_powertools import Logger

# Default log level when LOG_LEVEL is not set in the environment. INFO is the
# right operational default; set LOG_LEVEL=DEBUG in a stack parameter to get
# verbose detail without a code change.
DEFAULT_LOG_LEVEL = "INFO"


def get_logger(service_name: str) -> Logger:
    """Create a Powertools ``Logger`` for a PULSE component.

    The returned logger emits structured JSON and, inside a Lambda invocation,
    is automatically enriched with the function name, request id, and cold-start
    flag when decorated with ``@logger.inject_lambda_context`` on the handler.

    Args:
        service_name: The component/service name to tag every log line with,
            e.g. ``"pulse-rule-evaluator"``. Surfaces as the ``service`` key.

    Returns:
        A configured Powertools ``Logger`` instance. The log level is read from
        the ``LOG_LEVEL`` environment variable, defaulting to ``INFO``.
    """
    # Resource-affecting configuration comes from the environment, never
    # hardcoded (PYQUALITY-06). Powertools also honors POWERTOOLS_LOG_LEVEL;
    # we read LOG_LEVEL explicitly so the behavior is obvious and testable.
    log_level = os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    return Logger(service=service_name, level=log_level)


__all__ = ["get_logger", "DEFAULT_LOG_LEVEL"]
