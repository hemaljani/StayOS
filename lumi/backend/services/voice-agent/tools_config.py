"""Tool configuration schemas for the Nova Sonic voice agent.

Defines the JSON schemas for five read-only tools passed in the promptStart
event's toolConfiguration field. These schemas tell Nova 2 Sonic what tools are
available and what parameters they accept, enabling the model to invoke them
during conversation.

CRITICAL: inputSchema.json must be a JSON STRING, not a nested dict.
This matches the working AgentCore sample at:
https://github.com/awslabs/agentcore-samples/blob/main/06-workshops/01-AgentCore-runtime/06-bi-directional-streaming/01-bedrock-sonic-ws/websocket/s2s_events.py

Tool names use snake_case per Nova Sonic best practices.
Property ID is intentionally excluded from tool parameters - it is injected
from the authenticated session context to enforce property-scoped access.

Role in project: This module is imported by nova_sonic_session.py when
constructing the promptStart event sent to Nova Sonic at session initialization.
"""

import json
from typing import Any, Dict, List

# Tool schemas following the Nova 2 Sonic toolSpec format.
# inputSchema.json is a JSON STRING (not a nested object).
TOOL_CONFIGURATION: List[Dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "get_occupancy",
            "description": (
                "Get today's occupancy metrics including occupancy percentage, "
                "total arrivals, total departures, and available rooms for the "
                "property. Optionally accepts a date to query a specific day."
            ),
            "inputSchema": {
                "json": json.dumps({
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": (
                                "ISO 8601 date string (YYYY-MM-DD) to query. "
                                "Defaults to today if not provided."
                            ),
                        },
                    },
                    "required": [],
                })
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_revenue",
            "description": (
                "Get revenue key performance indicators including ADR "
                "(Average Daily Rate), RevPAR (Revenue Per Available Room), "
                "and comparisons versus budget and prior periods."
            ),
            "inputSchema": {
                "json": json.dumps({
                    "type": "object",
                    "properties": {
                        "start_date": {
                            "type": "string",
                            "description": (
                                "ISO 8601 start date (YYYY-MM-DD) for the "
                                "revenue period. Defaults to today."
                            ),
                        },
                        "end_date": {
                            "type": "string",
                            "description": (
                                "ISO 8601 end date (YYYY-MM-DD) for the "
                                "revenue period. Defaults to start_date."
                            ),
                        },
                    },
                    "required": [],
                })
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_vip_guests",
            "description": (
                "Get VIP guest arrivals and currently in-house VIP guests "
                "for the property. Returns guest names, loyalty tier, "
                "room assignment, and any noted occasion."
            ),
            "inputSchema": {
                "json": json.dumps({
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": (
                                "ISO 8601 date string (YYYY-MM-DD) for VIP "
                                "arrivals. Defaults to today."
                            ),
                        },
                    },
                    "required": [],
                })
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_room_status",
            "description": (
                "Get current room status including rooms out of order, "
                "out of service, or awaiting housekeeping. Returns room "
                "numbers, issue descriptions, and duration."
            ),
            "inputSchema": {
                "json": json.dumps({
                    "type": "object",
                    "properties": {},
                    "required": [],
                })
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_work_orders",
            "description": (
                "Get open and overdue maintenance work orders for the "
                "property. Returns work order IDs, descriptions, priority "
                "levels, and age in hours."
            ),
            "inputSchema": {
                "json": json.dumps({
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["OPEN", "IN_PROGRESS"],
                            "description": (
                                "Filter by status. If not provided, "
                                "returns both open and in-progress."
                            ),
                        },
                    },
                    "required": [],
                })
            },
        }
    },
]
