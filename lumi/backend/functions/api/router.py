"""LUMI API request router.

Provides lightweight path-based routing for API Gateway Lambda proxy events.
Matches HTTP method + path pattern and dispatches to handler functions,
extracting path parameters into a dictionary.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from aws_lambda_powertools import Logger

logger = Logger(service="stayos-api")

# Type alias for handler functions
HandlerFunc = Callable[[Dict[str, Any], Dict[str, str]], Dict[str, Any]]

# Route registry: list of (method, pattern, param_names, handler)
_routes: List[Tuple[str, re.Pattern, List[str], HandlerFunc]] = []


def route(method: str, path_pattern: str) -> Callable[[HandlerFunc], HandlerFunc]:
    """Decorator to register a handler function for a method + path pattern.

    Path parameters are specified with curly braces: /briefs/{propertyId}
    They are extracted and passed to the handler as a dict.

    Args:
        method: HTTP method (GET, PUT, POST, DELETE).
        path_pattern: URL path pattern with optional {param} placeholders.

    Returns:
        Decorator that registers the handler and returns it unchanged.

    Example:
        @route("GET", "/v1/briefs/{propertyId}")
        def get_brief(event, params):
            property_id = params["propertyId"]
            ...
    """
    # Extract parameter names from the pattern
    param_names = re.findall(r"\{(\w+)\}", path_pattern)

    # Convert path pattern to regex: {param} becomes a named capture group
    regex_pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path_pattern)
    compiled_pattern = re.compile(f"^{regex_pattern}$")

    def decorator(func: HandlerFunc) -> HandlerFunc:
        _routes.append((method.upper(), compiled_pattern, param_names, func))
        return func

    return decorator


def dispatch(event: Dict[str, Any]) -> Optional[Tuple[HandlerFunc, Dict[str, str]]]:
    """Match an API Gateway event to a registered route.

    Matches the event's HTTP method and resource path against registered
    routes. Returns the handler function and extracted path parameters.

    Args:
        event: API Gateway Lambda proxy event containing httpMethod and path.

    Returns:
        Tuple of (handler_function, path_params_dict) if matched, else None.
    """
    http_method = event.get("httpMethod", "").upper()
    path = event.get("path", "")

    for method, pattern, param_names, handler in _routes:
        if method != http_method:
            continue

        match = pattern.match(path)
        if match:
            params = match.groupdict()
            logger.debug(
                "Route matched",
                extra={"method": http_method, "path": path, "params": params},
            )
            return handler, params

    logger.warning(
        "No route matched",
        extra={"method": http_method, "path": path},
    )
    return None
