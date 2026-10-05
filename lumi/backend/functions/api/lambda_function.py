"""LUMI API Lambda function entry point.

Single Lambda handling all REST API routes via path-based routing.
Supports GET /v1/briefs/{propertyId}, GET /v1/settings/{gmAlias},
and PUT /v1/settings/{gmAlias}. Uses Cognito authorizer claims for
identity and ownership validation.
"""

import json
import os
from typing import Any, Dict

from aws_lambda_powertools import Logger, Tracer

from exceptions import ApiError, InternalError
from router import dispatch

# Import handlers to trigger route registration via decorators.
# history_handler MUST be imported before briefs_handler so the exact-match
# /briefs/history route registers before the {propertyId} param capture route.
import handlers.history_handler  # noqa: F401
import handlers.briefs_handler  # noqa: F401
import handlers.settings_handler  # noqa: F401

# Module-level logger configured before handler per PYQUALITY-03
logger = Logger(service="stayos-api")

# Module-level tracer for X-Ray distributed tracing (REQ-TEL-3)
tracer = Tracer(service="stayos-api")

# Read configuration from environment variables per PYQUALITY-06
BRIEFS_TABLE_NAME = os.environ.get("BRIEFS_TABLE_NAME", "")
SETTINGS_TABLE_NAME = os.environ.get("SETTINGS_TABLE_NAME", "")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")


def _build_response(status_code: int, body: Any) -> Dict[str, Any]:
    """Build an API Gateway Lambda proxy response.

    Constructs a response dict with the required fields for API Gateway
    Lambda proxy integration: statusCode, headers (CORS + JSON), and body.

    Args:
        status_code: HTTP status code for the response.
        body: Response body (will be JSON-serialized).

    Returns:
        API Gateway Lambda proxy response dictionary.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": FRONTEND_ORIGIN,
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


@tracer.capture_lambda_handler
@logger.inject_lambda_context
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """API Gateway Lambda proxy handler for all LUMI REST endpoints.

    Dispatches requests to registered route handlers based on HTTP method
    and path. Catches ApiError exceptions and returns formatted error responses.
    Extracts Cognito authorizer claims for identity context.

    Args:
        event: API Gateway Lambda proxy event containing httpMethod, path,
            body, pathParameters, and requestContext with authorizer claims.
        context: Lambda context object (runtime metadata).

    Returns:
        API Gateway Lambda proxy response with statusCode, headers, and body.
    """
    # Add request path metadata for X-Ray trace visibility
    tracer.put_metadata("request_path", event.get("path"))

    logger.info(
        "API request received",
        extra={
            "method": event.get("httpMethod"),
            "path": event.get("path"),
        },
    )

    try:
        # Dispatch to the matched route handler
        result = dispatch(event)

        if result is None:
            return _build_response(404, {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"No route matches {event.get('httpMethod')} {event.get('path')}",
                }
            })

        handler_func, path_params = result
        response_body = handler_func(event, path_params)

        # Add response status metadata to trace for success path
        tracer.put_metadata("response_status", 200)
        return _build_response(200, response_body)

    except ApiError as exc:
        # Known API errors - return structured error response
        logger.warning(
            "API error",
            extra={
                "error_code": exc.error_code,
                "status_code": exc.status_code,
                "error_message": exc.message,
            },
        )
        return _build_response(exc.status_code, exc.to_response_body())

    except Exception as exc:
        # Unexpected errors - log full traceback and return 500
        logger.error(
            "Unhandled exception in API handler",
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        internal_error = InternalError()
        return _build_response(500, internal_error.to_response_body())
