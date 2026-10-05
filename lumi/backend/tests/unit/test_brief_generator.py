"""Unit tests for the LUMI brief generator module.

Tests template selection by language, prompt variable injection,
Bedrock invocation with retry logic, and fallback narrative generation.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from brief_generator import (
    DEFAULT_LANGUAGE,
    LANGUAGE_TEMPLATE_MAP,
    _build_action_items_summary,
    _fill_template_variables,
    _generate_fallback_narrative,
    _get_language_preference,
    _invoke_bedrock_with_retry,
    _load_prompt_template,
    generate_brief_narrative,
)
from orchestrator_exceptions import BriefGenerationError


# -- Test fixtures --


@pytest.fixture()
def sample_property_data() -> dict:
    """Provide sample property data for testing."""
    return {
        "property": {
            "propertyId": "ALOHA-CHI-001",
            "propertyName": "Aloha Grand Chicago",
            "gmAlias": "jsmith",
            "gmName": "Jennifer Smith",
        },
        "dailyKPIs": {
            "occupancy": {
                "current": 87,
                "vsLastWeek": 4.2,
                "forecast3pm": 91,
            },
            "adr": {
                "current": 248,
                "vsLastWeek": 12,
                "pacePctOfBudget": 103,
            },
            "revPAR": {
                "current": 216,
                "vsYOY": 7.1,
                "budget": 202,
            },
            "arrivals": {
                "vipCount": 7,
                "ambassadorCount": 3,
                "titaniumCount": 4,
            },
        },
        "actionItems": [
            {"id": "a1", "severity": "URGENT", "title": "Overbooking Risk - +6 Rooms"},
            {"id": "a2", "severity": "HIGH", "title": "Ambassador VIP - David Chen"},
            {"id": "a3", "severity": "MEDIUM", "title": "Upsell Opportunity"},
        ],
        "vipArrivals": [],
    }


@pytest.fixture()
def sample_settings_en() -> dict:
    """Provide sample settings with English language preference."""
    return {
        "gmName": "Jennifer Smith",
        "audioPreferences": {
            "language": "en-US",
            "briefLength": "standard",
        },
    }


@pytest.fixture()
def sample_settings_ja() -> dict:
    """Provide sample settings with Japanese language preference."""
    return {
        "gmName": "Takeshi Yamamoto",
        "audioPreferences": {
            "language": "ja-JP",
            "briefLength": "standard",
        },
    }


# -- Tests for language preference extraction --


class TestGetLanguagePreference:
    """Tests for language preference extraction from settings."""

    def test_returns_configured_language(self) -> None:
        """Returns the language from audioPreferences."""
        settings = {"audioPreferences": {"language": "ja-JP"}}
        assert _get_language_preference(settings) == "ja-JP"

    def test_defaults_to_english_when_missing(self) -> None:
        """Defaults to en-US when no audioPreferences key."""
        settings = {}
        assert _get_language_preference(settings) == DEFAULT_LANGUAGE

    def test_defaults_to_english_for_unsupported(self) -> None:
        """Defaults to en-US for unsupported language codes."""
        settings = {"audioPreferences": {"language": "fr-FR"}}
        assert _get_language_preference(settings) == DEFAULT_LANGUAGE

    def test_all_supported_languages(self) -> None:
        """All supported languages are accepted."""
        for lang_code in LANGUAGE_TEMPLATE_MAP:
            settings = {"audioPreferences": {"language": lang_code}}
            assert _get_language_preference(settings) == lang_code


# -- Tests for prompt template loading --


class TestLoadPromptTemplate:
    """Tests for prompt template file loading."""

    def test_loads_english_template(self) -> None:
        """English template loads and contains expected placeholders."""
        template = _load_prompt_template("en-US")
        assert "{gm_name}" in template
        assert "{occupancy_current}" in template
        assert "{action_items_summary}" in template

    def test_loads_spanish_template(self) -> None:
        """Spanish template loads with Spanish instructions."""
        template = _load_prompt_template("es-ES")
        assert "{gm_name}" in template
        assert "Buenos dias" in template

    def test_loads_japanese_template(self) -> None:
        """Japanese template loads with Japanese content."""
        template = _load_prompt_template("ja-JP")
        assert "{gm_name}" in template

    def test_loads_chinese_template(self) -> None:
        """Chinese template loads with Chinese content."""
        template = _load_prompt_template("zh-CN")
        assert "{gm_name}" in template

    def test_missing_template_raises_error(self) -> None:
        """BriefGenerationError raised for non-existent template."""
        with patch.dict(LANGUAGE_TEMPLATE_MAP, {"xx-XX": "nonexistent.txt"}):
            with pytest.raises(BriefGenerationError) as exc_info:
                _load_prompt_template("xx-XX")
            assert "template" in exc_info.value.stage


# -- Tests for template variable filling --


class TestFillTemplateVariables:
    """Tests for prompt variable injection."""

    def test_fills_gm_name(self, sample_property_data: dict, sample_settings_en: dict) -> None:
        """GM name is substituted into the template."""
        template = "Hello {gm_name}, welcome to {property_name}."
        result = _fill_template_variables(template, sample_property_data, sample_settings_en)

        assert "Jennifer Smith" in result
        assert "Aloha Grand Chicago" in result

    def test_fills_gm_first_name(self, sample_property_data: dict, sample_settings_en: dict) -> None:
        """GM first name is extracted and substituted."""
        template = "Good morning, {gm_first_name}."
        result = _fill_template_variables(template, sample_property_data, sample_settings_en)

        assert "Jennifer" in result
        assert "Smith" not in result

    def test_fills_kpi_values(self, sample_property_data: dict, sample_settings_en: dict) -> None:
        """KPI values are correctly substituted."""
        template = "Occupancy: {occupancy_current}%, ADR: {adr_current}"
        result = _fill_template_variables(template, sample_property_data, sample_settings_en)

        assert "87" in result
        assert "248" in result

    def test_fills_vip_counts(self, sample_property_data: dict, sample_settings_en: dict) -> None:
        """VIP counts are substituted."""
        template = "{vip_count} VIPs ({ambassador_count} Ambassador)"
        result = _fill_template_variables(template, sample_property_data, sample_settings_en)

        assert "7" in result
        assert "3" in result

    def test_fills_action_summary(self, sample_property_data: dict, sample_settings_en: dict) -> None:
        """Action items summary is generated and substituted."""
        template = "Actions: {action_items_summary}"
        result = _fill_template_variables(template, sample_property_data, sample_settings_en)

        assert "URGENT" in result
        assert "Overbooking" in result


# -- Tests for action items summary builder --


class TestBuildActionItemsSummary:
    """Tests for action items summary generation."""

    def test_builds_summary_from_items(self) -> None:
        """Summary includes severity and title for top items."""
        items = [
            {"severity": "URGENT", "title": "Overbooking Risk"},
            {"severity": "HIGH", "title": "VIP Alert"},
        ]
        result = _build_action_items_summary(items)

        assert "[URGENT] Overbooking Risk" in result
        assert "[HIGH] VIP Alert" in result

    def test_limits_to_three_items(self) -> None:
        """Summary only includes top 3 items."""
        items = [
            {"severity": "URGENT", "title": "Item 1"},
            {"severity": "HIGH", "title": "Item 2"},
            {"severity": "MEDIUM", "title": "Item 3"},
            {"severity": "LOW", "title": "Item 4"},
        ]
        result = _build_action_items_summary(items)

        assert "Item 4" not in result

    def test_empty_items_returns_message(self) -> None:
        """Empty action items returns a 'no items' message."""
        result = _build_action_items_summary([])
        assert "No action items today" in result


# -- Tests for Bedrock invocation --


class TestInvokeBedrockWithRetry:
    """Tests for Bedrock API invocation with retry logic."""

    def test_successful_invocation(self) -> None:
        """Returns narrative on successful Bedrock response."""
        mock_response = {
            "output": {
                "message": {
                    "content": [{"text": "Good morning, Jennifer."}]
                }
            }
        }

        with patch("brief_generator._bedrock_client") as mock_client:
            mock_client.converse.return_value = mock_response
            mock_client.exceptions = MagicMock()

            result = _invoke_bedrock_with_retry("Test prompt")

        assert result == "Good morning, Jennifer."

    def test_retries_on_throttling(self) -> None:
        """Retries once on ThrottlingException then succeeds."""
        throttle_exc = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Converse",
        )
        success_response = {
            "output": {
                "message": {
                    "content": [{"text": "Success after retry."}]
                }
            }
        }

        with patch("brief_generator._bedrock_client") as mock_client:
            # Create a proper exception class on the mock
            mock_client.exceptions.ThrottlingException = type(
                "ThrottlingException", (ClientError,), {}
            )
            mock_client.exceptions.ModelTimeoutException = type(
                "ModelTimeoutException", (ClientError,), {}
            )
            mock_client.exceptions.AccessDeniedException = type(
                "AccessDeniedException", (ClientError,), {}
            )
            mock_client.converse.side_effect = [
                mock_client.exceptions.ThrottlingException(
                    {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                    "Converse",
                ),
                success_response,
            ]

            with patch("brief_generator.time.sleep"):
                result = _invoke_bedrock_with_retry("Test prompt")

        assert result == "Success after retry."

    def test_returns_empty_on_access_denied(self) -> None:
        """Returns empty string immediately on AccessDeniedException."""
        with patch("brief_generator._bedrock_client") as mock_client:
            mock_client.exceptions.ThrottlingException = type(
                "ThrottlingException", (ClientError,), {}
            )
            mock_client.exceptions.ModelTimeoutException = type(
                "ModelTimeoutException", (ClientError,), {}
            )
            mock_client.exceptions.AccessDeniedException = type(
                "AccessDeniedException", (ClientError,), {}
            )
            mock_client.converse.side_effect = mock_client.exceptions.AccessDeniedException(
                {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
                "Converse",
            )

            result = _invoke_bedrock_with_retry("Test prompt")

        assert result == ""

    def test_returns_empty_after_all_retries_exhausted(self) -> None:
        """Returns empty string when all retry attempts fail."""
        with patch("brief_generator._bedrock_client") as mock_client:
            mock_client.exceptions.ThrottlingException = type(
                "ThrottlingException", (ClientError,), {}
            )
            mock_client.exceptions.ModelTimeoutException = type(
                "ModelTimeoutException", (ClientError,), {}
            )
            mock_client.exceptions.AccessDeniedException = type(
                "AccessDeniedException", (ClientError,), {}
            )
            # Both attempts raise throttling
            mock_client.converse.side_effect = [
                mock_client.exceptions.ThrottlingException(
                    {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                    "Converse",
                ),
                mock_client.exceptions.ThrottlingException(
                    {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                    "Converse",
                ),
            ]

            with patch("brief_generator.time.sleep"):
                result = _invoke_bedrock_with_retry("Test prompt")

        assert result == ""


# -- Tests for fallback narrative --


class TestFallbackNarrative:
    """Tests for template-based fallback narrative generation."""

    def test_english_fallback_contains_kpis(
        self, sample_property_data: dict, sample_settings_en: dict
    ) -> None:
        """English fallback includes KPI data."""
        result = _generate_fallback_narrative(
            sample_property_data, sample_settings_en, "en-US"
        )

        assert "Jennifer" in result
        assert "87" in result  # occupancy
        assert "248" in result  # ADR

    def test_japanese_fallback_generated(
        self, sample_property_data: dict, sample_settings_ja: dict
    ) -> None:
        """Japanese fallback is generated with Japanese characters."""
        result = _generate_fallback_narrative(
            sample_property_data, sample_settings_ja, "ja-JP"
        )

        # Should contain Japanese greeting pattern
        assert "おはようございます" in result

    def test_fallback_defaults_to_english_for_unknown(
        self, sample_property_data: dict, sample_settings_en: dict
    ) -> None:
        """Unknown language falls back to English template."""
        result = _generate_fallback_narrative(
            sample_property_data, sample_settings_en, "fr-FR"
        )

        assert "Good morning" in result


# -- Tests for full generate_brief_narrative flow --


class TestGenerateBriefNarrative:
    """Tests for the complete brief generation pipeline."""

    def test_uses_bedrock_when_available(
        self, sample_property_data: dict, sample_settings_en: dict
    ) -> None:
        """Returns Bedrock-generated narrative when API succeeds."""
        mock_narrative = "Good morning, Jennifer. Your property is performing well today."

        with patch(
            "brief_generator._invoke_bedrock_with_retry",
            return_value=mock_narrative,
        ):
            result = generate_brief_narrative(sample_property_data, sample_settings_en)

        assert result == mock_narrative

    def test_falls_back_when_bedrock_fails(
        self, sample_property_data: dict, sample_settings_en: dict
    ) -> None:
        """Returns fallback narrative when Bedrock returns empty."""
        with patch(
            "brief_generator._invoke_bedrock_with_retry",
            return_value="",
        ):
            result = generate_brief_narrative(sample_property_data, sample_settings_en)

        # Fallback should contain GM name and KPI data
        assert "Jennifer" in result
        assert "87" in result

    def test_respects_language_preference(
        self, sample_property_data: dict, sample_settings_ja: dict
    ) -> None:
        """Uses Japanese fallback when language is ja-JP and Bedrock fails."""
        # Update property data with Japanese GM name
        sample_property_data["property"]["gmName"] = "Takeshi Yamamoto"

        with patch(
            "brief_generator._invoke_bedrock_with_retry",
            return_value="",
        ):
            result = generate_brief_narrative(sample_property_data, sample_settings_ja)

        assert "おはようございます" in result
