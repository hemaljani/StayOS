"""X-Ray tracing factory for PULSE components.

PULSE enables AWS X-Ray on every runtime Lambda via ``TracingConfig: Active``
(set in the pipeline/api CloudFormation stacks). This module provides a single
factory so each component initializes an AWS Lambda Powertools ``Tracer``
consistently with a service name, mirroring the ``get_logger`` factory in
``pulse.common.logging``. The Powertools ``Tracer`` is what enriches the raw
X-Ray segments with handler-level captures (``@tracer.capture_lambda_handler``,
``@tracer.capture_method``) and correlation annotations
(``tracer.put_annotation``), matching the LUMI reference pattern.

Usage (at module level in a component, before the handler):

    from pulse.common.logging import get_logger
    from pulse.common.tracing import get_tracer

    logger = get_logger("pulse-rule-evaluator")
    tracer = get_tracer("pulse-rule-evaluator")

    @tracer.capture_lambda_handler
    @logger.inject_lambda_context
    def lambda_handler(event, context):
        ...

Safety: the Powertools ``Tracer`` is a transparent no-op when the X-Ray SDK or
daemon is not present (for example in unit tests or local runs). The capture
decorators still wrap the function and simply pass through, so importing or
using this factory never requires the X-Ray SDK to be installed or active.
"""

from __future__ import annotations

from aws_lambda_powertools import Tracer


def get_tracer(service_name: str) -> Tracer:
    """Create a Powertools ``Tracer`` for a PULSE component.

    The returned tracer segments and annotations are tagged with
    ``service_name`` so traces are attributable per component in the X-Ray
    service map. Outside a Lambda/X-Ray environment (for example under pytest)
    the tracer auto-disables and its decorators become transparent pass-throughs,
    so this is safe to call at import time in every component.

    Args:
        service_name: The component/service name to tag trace segments with,
            e.g. ``"pulse-rule-evaluator"``. Should match the name passed to
            :func:`pulse.common.logging.get_logger` for the same component.

    Returns:
        A configured Powertools ``Tracer`` instance for the component.
    """
    # Powertools also honors the POWERTOOLS_SERVICE_NAME environment variable;
    # we pass the service name explicitly so the behavior is obvious and each
    # component's traces are attributable without extra configuration.
    return Tracer(service=service_name)


__all__ = ["get_tracer"]
