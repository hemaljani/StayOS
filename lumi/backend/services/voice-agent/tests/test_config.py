"""Unit tests for voice agent system prompt and tool configuration.

Validates that the system prompt enforces hotel operations scope, protects
sensitive data, prevents write operations, and correctly constrains Nova
Sonic's behavior. Also validates tool configuration structure, naming, and
read-only posture.

Validates: Requirements 5.1, 5.3, 9.4
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

import json

import pytest

# Add the voice-agent service directory to the path so we can import modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system_prompt import SYSTEM_PROMPT
from tools_config import TOOL_CONFIGURATION


# ---------------------------------------------------------------------------
# Expected tool names (from Requirements 3.1)
# ---------------------------------------------------------------------------

EXPECTED_TOOL_NAMES: List[str] = [
    "get_occupancy",
    "get_revenue",
    "get_vip_guests",
    "get_room_status",
    "get_work_orders",
]

# Keywords that indicate write operations — none should appear in tool schemas
WRITE_KEYWORDS: List[str] = [
    "write",
    "create",
    "update",
    "delete",
    "put",
    "remove",
]


# ---------------------------------------------------------------------------
# System Prompt Tests
# ---------------------------------------------------------------------------


class TestSystemPromptConstraints:
    """Tests verifying the system prompt constrains Nova Sonic behavior."""

    def test_system_prompt_constrains_to_hotel_operations(self) -> None:
        """Verify SYSTEM_PROMPT scopes responses to hotel operations data.

        The system prompt must include phrasing that limits Nova Sonic to
        answering only questions about the property's operational data.
        """
        prompt_lower = SYSTEM_PROMPT.lower()
        # The prompt should reference hotel operations scope
        assert "hotel operations" in prompt_lower, (
            "System prompt must contain 'hotel operations' to constrain scope"
        )

    def test_system_prompt_excludes_sensitive_notes(self) -> None:
        """Verify SYSTEM_PROMPT explicitly forbids reading sensitiveNotes.

        Requirement 5.3: Never read aloud guest sensitiveNotes fields.
        """
        assert "sensitiveNotes" in SYSTEM_PROMPT, (
            "System prompt must mention 'sensitiveNotes' as a forbidden field"
        )

    def test_system_prompt_no_write_operations(self) -> None:
        """Verify SYSTEM_PROMPT instructs model to never write or modify data.

        Requirement 9.4: System prompt shall not instruct Nova Sonic to
        perform any write or mutation actions.
        """
        prompt_lower = SYSTEM_PROMPT.lower()
        # The prompt must explicitly state it is read-only or prohibit writes
        assert "read-only" in prompt_lower or "read only" in prompt_lower, (
            "System prompt must contain 'read-only' instruction"
        )
        # Also verify it mentions not performing actions
        assert "never" in prompt_lower and (
            "create" in prompt_lower or "update" in prompt_lower or "delete" in prompt_lower
        ), (
            "System prompt must explicitly prohibit create/update/delete actions"
        )

    def test_system_prompt_unavailability_response(self) -> None:
        """Verify SYSTEM_PROMPT contains the exact unavailability phrasing.

        When a tool returns an unavailability indicator, the model should
        say exactly this phrase rather than guessing.
        """
        assert "I don't have that data right now" in SYSTEM_PROMPT, (
            "System prompt must contain exact phrase: "
            "\"I don't have that data right now\""
        )

    def test_system_prompt_identity(self) -> None:
        """Verify SYSTEM_PROMPT establishes the StayOS voice assistant identity."""
        assert "StayOS voice assistant" in SYSTEM_PROMPT, (
            "System prompt must identify the agent as 'StayOS voice assistant'"
        )


# ---------------------------------------------------------------------------
# Tool Configuration Tests
# ---------------------------------------------------------------------------


class TestToolConfiguration:
    """Tests verifying tool configuration structure and read-only posture."""

    def test_tool_configuration_has_five_tools(self) -> None:
        """Verify exactly 5 tools are defined in the configuration.

        Requirements 3.1: Five tools must be configured for Nova Sonic.
        """
        assert len(TOOL_CONFIGURATION) == 5, (
            f"Expected 5 tools, found {len(TOOL_CONFIGURATION)}"
        )

    def test_tool_configuration_correct_names(self) -> None:
        """Verify all expected tool names are present in the configuration."""
        actual_names = [
            tool["toolSpec"]["name"] for tool in TOOL_CONFIGURATION
        ]
        assert sorted(actual_names) == sorted(EXPECTED_TOOL_NAMES), (
            f"Tool names mismatch.\n"
            f"Expected: {sorted(EXPECTED_TOOL_NAMES)}\n"
            f"Actual:   {sorted(actual_names)}"
        )

    def test_tool_schemas_no_write_parameters(self) -> None:
        """Property 12: Tool Schemas Are Read-Only.

        Verify no tool schema parameter name or description references
        write/create/update/delete/put/remove operations.

        Validates: Requirements 9.4
        """
        for tool in TOOL_CONFIGURATION:
            tool_name = tool["toolSpec"]["name"]
            schema_raw = tool["toolSpec"]["inputSchema"]["json"]
            # inputSchema.json is a JSON string in Nova 2 Sonic format
            schema = json.loads(schema_raw) if isinstance(schema_raw, str) else schema_raw
            properties = schema.get("properties", {})

            for param_name, param_spec in properties.items():
                param_name_lower = param_name.lower()
                param_desc_lower = param_spec.get("description", "").lower()

                for keyword in WRITE_KEYWORDS:
                    assert keyword not in param_name_lower, (
                        f"Tool '{tool_name}' parameter '{param_name}' "
                        f"contains write keyword '{keyword}'"
                    )
                    assert keyword not in param_desc_lower, (
                        f"Tool '{tool_name}' parameter '{param_name}' "
                        f"description contains write keyword '{keyword}'"
                    )

    def test_tool_schemas_no_property_id_parameter(self) -> None:
        """Verify no tool exposes a propertyId parameter.

        Property ID must be injected from authenticated session context,
        never exposed as a model-controllable parameter.
        """
        for tool in TOOL_CONFIGURATION:
            tool_name = tool["toolSpec"]["name"]
            schema_raw = tool["toolSpec"]["inputSchema"]["json"]
            schema = json.loads(schema_raw) if isinstance(schema_raw, str) else schema_raw
            properties = schema.get("properties", {})

            param_names_lower = [p.lower() for p in properties.keys()]
            assert "propertyid" not in param_names_lower, (
                f"Tool '{tool_name}' must not have a propertyId parameter - "
                "it is injected from session context"
            )

    def test_tool_schemas_valid_structure(self) -> None:
        """Verify each tool has the required Nova Sonic toolSpec structure.

        Each tool must have: toolSpec.name, toolSpec.description,
        toolSpec.inputSchema.json with type and properties.
        """
        for i, tool in enumerate(TOOL_CONFIGURATION):
            # toolSpec must exist
            assert "toolSpec" in tool, (
                f"Tool at index {i} missing 'toolSpec' key"
            )
            spec = tool["toolSpec"]

            # name must be a non-empty string
            assert "name" in spec and isinstance(spec["name"], str) and spec["name"], (
                f"Tool at index {i} missing or empty 'toolSpec.name'"
            )

            # description must be a non-empty string
            assert "description" in spec and isinstance(spec["description"], str) and spec["description"], (
                f"Tool '{spec.get('name', i)}' missing or empty 'toolSpec.description'"
            )

            # inputSchema.json must exist with type and properties
            assert "inputSchema" in spec, (
                f"Tool '{spec['name']}' missing 'toolSpec.inputSchema'"
            )
            assert "json" in spec["inputSchema"], (
                f"Tool '{spec['name']}' missing 'toolSpec.inputSchema.json'"
            )
            json_schema_raw = spec["inputSchema"]["json"]
            json_schema = json.loads(json_schema_raw) if isinstance(json_schema_raw, str) else json_schema_raw
            assert json_schema.get("type") == "object", (
                f"Tool '{spec['name']}' inputSchema.json.type must be 'object'"
            )
            assert "properties" in json_schema, (
                f"Tool '{spec['name']}' inputSchema.json missing 'properties'"
            )
