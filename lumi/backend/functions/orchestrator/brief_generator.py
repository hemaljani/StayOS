"""LUMI Brief Generator - AI narrative generation via Amazon Bedrock.

Generates personalized morning briefing narratives for hotel General Managers
using Amazon Bedrock (Claude 3.5 Sonnet) with language-specific prompt templates.
Includes retry logic and template-based fallback for resilience.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import ClientError

from orchestrator_exceptions import BriefGenerationError

logger = Logger(service="stayos-orchestrator")

# Module-level configuration from environment variables
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# Module-level boto3 client for Bedrock Runtime (connection reuse across invocations)
_bedrock_config = Config(
    retries={"total_max_attempts": 2, "mode": "standard"},
    connect_timeout=10,
    read_timeout=60,
)
_bedrock_client = boto3.client("bedrock-runtime", config=_bedrock_config)

# Prompt template directory path (relative to this module)
_TEMPLATE_DIR = Path(__file__).parent / "prompt_templates"

# Language code to template file mapping
LANGUAGE_TEMPLATE_MAP: Dict[str, str] = {
    "en-US": "brief_en_us.txt",
    "es-ES": "brief_es_es.txt",
    "ja-JP": "brief_ja_jp.txt",
    "zh-CN": "brief_zh_cn.txt",
}

# Default language when preference is not set
DEFAULT_LANGUAGE = "en-US"

# Brief length to word/duration target mapping
BRIEF_LENGTH_TARGETS: Dict[str, Dict[str, str]] = {
    "brief": {
        "en-US": "Target 80-120 words (about 60 seconds when read aloud). Be concise - cover only the most critical items.",
        "es-ES": "Objetivo: 80-120 palabras (unos 60 segundos al leer en voz alta). Se conciso - cubre solo lo mas critico.",
        "ja-JP": "目標: 簡潔に60秒程度。最も重要な項目のみを伝えてください。",
        "zh-CN": "目标: 80-120字（朗读约60秒）。简明扼要，只涵盖最关键的事项。",
    },
    "standard": {
        "en-US": "Target 150-225 words (about 90 seconds when read aloud).",
        "es-ES": "Objetivo: 150-225 palabras (unos 90 segundos al leer en voz alta).",
        "ja-JP": "目標: 音読で約90秒の長さ。",
        "zh-CN": "目标: 150-225字（朗读约90秒）。",
    },
    "detailed": {
        "en-US": "Target 250-350 words (about 120 seconds when read aloud). Provide additional context and nuance for each KPI and action item.",
        "es-ES": "Objetivo: 250-350 palabras (unos 120 segundos al leer en voz alta). Proporciona contexto adicional y matices para cada KPI y accion.",
        "ja-JP": "目標: 音読で約120秒の長さ。各KPIとアクション項目に追加の背景と詳細を提供してください。",
        "zh-CN": "目标: 250-350字（朗读约120秒）。为每个KPI和行动事项提供额外的背景和细节。",
    },
}

# Default brief length when preference is not set
DEFAULT_BRIEF_LENGTH = "standard"

# Retry configuration
MAX_RETRIES = 1
RETRY_DELAY_SECONDS = 2


def generate_brief_narrative(
    property_data: Dict[str, Any], settings: Dict[str, Any]
) -> str:
    """Generate an AI-powered morning briefing narrative for a GM.

    Selects the prompt template based on the GM's language preference,
    fills template variables with property data, and calls Amazon Bedrock
    to generate a natural-language narrative.

    If Bedrock fails after one retry, falls back to a template-generated
    narrative using simple string interpolation (no AI).

    Args:
        property_data: Combined property data dict from data_puller containing
            property, dailyKPIs, actionItems, and vipArrivals.
        settings: GM settings dict including audioPreferences.language.

    Returns:
        Generated narrative string suitable for text-to-speech synthesis.

    Raises:
        BriefGenerationError: Only if both Bedrock and fallback template fail
            (should not happen in practice since fallback is pure string ops).
    """
    language = _get_language_preference(settings)
    template_text = _load_prompt_template(language)
    filled_prompt = _fill_template_variables(template_text, property_data, settings)

    logger.info(
        "Generating brief narrative via Bedrock",
        language=language,
        model_id=BEDROCK_MODEL_ID,
        property_id=property_data.get("property", {}).get("propertyId", "unknown"),
    )

    # Attempt Bedrock generation with retry
    narrative = _invoke_bedrock_with_retry(filled_prompt)

    if narrative:
        return narrative

    # Bedrock failed after retries - use template fallback
    logger.warning(
        "Bedrock generation failed - using template fallback",
        property_id=property_data.get("property", {}).get("propertyId", "unknown"),
    )
    return _generate_fallback_narrative(property_data, settings, language)


def _get_language_preference(settings: Dict[str, Any]) -> str:
    """Extract the language preference from GM settings.

    Defaults to en-US if no preference is configured or if the
    configured language is not supported.

    Args:
        settings: GM settings dict.

    Returns:
        Language code string (e.g., "en-US").
    """
    audio_prefs = settings.get("audioPreferences", {})
    language = audio_prefs.get("language", DEFAULT_LANGUAGE)

    if language not in LANGUAGE_TEMPLATE_MAP:
        logger.warning(
            "Unsupported language preference - defaulting to en-US",
            configured_language=language,
        )
        return DEFAULT_LANGUAGE

    return language


def _get_brief_length_instruction(settings: Dict[str, Any], language: str) -> str:
    """Get the length instruction text based on the GM's briefLength preference.

    Maps the briefLength setting (brief/standard/detailed) to language-specific
    instruction text that controls how many words/seconds the AI targets.

    Args:
        settings: GM settings dict containing audioPreferences.briefLength.
        language: Already-validated language code from _get_language_preference.

    Returns:
        Length instruction string for the prompt template.
    """
    audio_prefs = settings.get("audioPreferences", {})
    brief_length = audio_prefs.get("briefLength", DEFAULT_BRIEF_LENGTH)

    # Validate brief_length value
    if brief_length not in BRIEF_LENGTH_TARGETS:
        logger.warning(
            "Unsupported briefLength - defaulting to standard",
            configured_length=brief_length,
        )
        brief_length = DEFAULT_BRIEF_LENGTH

    # Get the instruction for this length and language
    length_targets = BRIEF_LENGTH_TARGETS[brief_length]
    return length_targets.get(language, length_targets["en-US"])


def _load_prompt_template(language: str) -> str:
    """Load the prompt template file for the specified language.

    Args:
        language: Language code (e.g., "en-US").

    Returns:
        Raw template text with placeholder variables.

    Raises:
        BriefGenerationError: If the template file cannot be read.
    """
    template_filename = LANGUAGE_TEMPLATE_MAP.get(language, LANGUAGE_TEMPLATE_MAP[DEFAULT_LANGUAGE])
    template_path = _TEMPLATE_DIR / template_filename

    try:
        with open(template_path, "r", encoding="utf-8") as template_file:
            return template_file.read()
    except FileNotFoundError:
        raise BriefGenerationError(
            "template",
            f"Prompt template not found: {template_path}",
        )
    except OSError as error:
        raise BriefGenerationError(
            "template",
            f"Cannot read prompt template {template_path}: {error}",
        )


def _fill_template_variables(
    template_text: str, property_data: Dict[str, Any], settings: Dict[str, Any]
) -> str:
    """Fill prompt template placeholders with actual property data.

    Substitutes all {variable_name} placeholders in the template with
    values extracted from the property data and settings.

    Args:
        template_text: Raw template with {placeholder} variables.
        property_data: Combined property data from data_puller.
        settings: GM settings dict.

    Returns:
        Filled prompt string ready for Bedrock invocation.
    """
    prop = property_data.get("property", {})
    kpis = property_data.get("dailyKPIs", {})
    occupancy = kpis.get("occupancy", {})
    adr = kpis.get("adr", {})
    revpar = kpis.get("revPAR", {})
    arrivals = kpis.get("arrivals", {})
    action_items = property_data.get("actionItems", [])

    # Extract GM first name from full name
    gm_name = prop.get("gmName", settings.get("gmName", "Manager"))
    gm_first_name = gm_name.split()[0] if gm_name else "Manager"

    # Build action items summary (top 3 by severity)
    action_summary = _build_action_items_summary(action_items)

    # Format today's date in a human-readable format
    today_date = datetime.now(tz=timezone.utc).strftime("%A, %B %d, %Y")

    # Build the variable mapping for template substitution
    variables = {
        "gm_name": gm_name,
        "gm_first_name": gm_first_name,
        "property_name": prop.get("propertyName", "the property"),
        "date": today_date,
        "occupancy_current": str(occupancy.get("current", "N/A")),
        "occupancy_vs_lw": str(occupancy.get("vsLastWeek", "N/A")),
        "occupancy_forecast": str(occupancy.get("forecast3pm", "N/A")),
        "adr_current": str(adr.get("current", "N/A")),
        "adr_vs_lw": str(adr.get("vsLastWeek", "N/A")),
        "adr_pace": str(adr.get("pacePctOfBudget", "N/A")),
        "revpar_current": str(revpar.get("current", "N/A")),
        "revpar_yoy": str(revpar.get("vsYOY", "N/A")),
        "revpar_budget": str(revpar.get("budget", "N/A")),
        "vip_count": str(arrivals.get("vipCount", 0)),
        "ambassador_count": str(arrivals.get("ambassadorCount", 0)),
        "titanium_count": str(arrivals.get("titaniumCount", 0)),
        "action_items_summary": action_summary,
        "brief_length_instruction": _get_brief_length_instruction(settings, _get_language_preference(settings)),
    }

    # Use safe string formatting to avoid KeyError on template variables
    # that include $ signs (like ${adr_vs_lw} in the template)
    filled = template_text
    for key, value in variables.items():
        filled = filled.replace("{" + key + "}", value)

    return filled


def _build_action_items_summary(action_items: list) -> str:
    """Build a concise summary of the top action items for the prompt.

    Includes the top 3 action items sorted by severity, formatted as
    a semicolon-separated string.

    Args:
        action_items: List of action item dicts from property data.

    Returns:
        Semicolon-separated summary string of top actions.
    """
    if not action_items:
        return "No action items today."

    # Take top 3 items (already sorted by priority from action_prioritizer)
    top_items = action_items[:3]
    summaries = []

    for item in top_items:
        severity = item.get("severity", "LOW")
        title = item.get("title", "Unknown action")
        summaries.append(f"[{severity}] {title}")

    return "; ".join(summaries)


def _invoke_bedrock_with_retry(prompt: str) -> str:
    """Call Bedrock InvokeModel with one retry on failure.

    Uses the Converse API with Claude 3.5 Sonnet. On transient errors
    (throttling, timeout), retries once with exponential backoff.

    Args:
        prompt: The filled prompt text to send to the model.

    Returns:
        Generated narrative string, or empty string if all attempts fail.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            # Use the Converse API for Bedrock model invocation
            response = _bedrock_client.converse(
                modelId=BEDROCK_MODEL_ID,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ],
                inferenceConfig={
                    "maxTokens": 512,
                    "temperature": 0.7,
                },
            )

            # Extract the text response from the Converse API response
            output_message = response.get("output", {}).get("message", {})
            content_blocks = output_message.get("content", [])

            if content_blocks:
                narrative = content_blocks[0].get("text", "")
                if narrative:
                    logger.info(
                        "Bedrock narrative generated successfully",
                        attempt=attempt + 1,
                        response_length=len(narrative),
                    )
                    return narrative

            logger.warning("Bedrock returned empty response", attempt=attempt + 1)

        except _bedrock_client.exceptions.ThrottlingException:
            logger.warning(
                "Bedrock throttling - retrying",
                attempt=attempt + 1,
                max_retries=MAX_RETRIES,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

        except _bedrock_client.exceptions.ModelTimeoutException:
            logger.warning(
                "Bedrock model timeout - retrying",
                attempt=attempt + 1,
                max_retries=MAX_RETRIES,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

        except _bedrock_client.exceptions.AccessDeniedException as error:
            # Access denied is not retryable - log and fail immediately
            logger.error(
                "Bedrock access denied - cannot generate narrative",
                error=str(error),
            )
            return ""

        except ClientError as error:
            # Catch-all for unexpected Bedrock errors
            logger.error(
                "Unexpected Bedrock error",
                error=str(error),
                attempt=attempt + 1,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    logger.error("All Bedrock attempts exhausted")
    return ""


def _generate_fallback_narrative(
    property_data: Dict[str, Any], settings: Dict[str, Any], language: str
) -> str:
    """Generate a template-based fallback narrative without AI.

    Uses simple string interpolation to create a basic but accurate
    narrative when Bedrock is unavailable. No hallucination risk since
    only source data is used.

    Args:
        property_data: Combined property data from data_puller.
        settings: GM settings dict.
        language: Language code for the fallback template.

    Returns:
        Plain-text fallback narrative string.
    """
    prop = property_data.get("property", {})
    kpis = property_data.get("dailyKPIs", {})
    occupancy = kpis.get("occupancy", {})
    adr = kpis.get("adr", {})
    revpar = kpis.get("revPAR", {})
    arrivals = kpis.get("arrivals", {})
    action_items = property_data.get("actionItems", [])

    gm_name = prop.get("gmName", settings.get("gmName", "Manager"))
    gm_first_name = gm_name.split()[0] if gm_name else "Manager"
    today_date = datetime.now(tz=timezone.utc).strftime("%A, %B %d, %Y")

    # Count urgent/high items for the summary
    urgent_count = sum(1 for item in action_items if item.get("severity") == "URGENT")
    high_count = sum(1 for item in action_items if item.get("severity") == "HIGH")

    # Language-specific fallback templates
    fallback_templates = {
        "en-US": (
            f"Good morning, {gm_first_name}. Today is {today_date}. "
            f"Your property is at {occupancy.get('current', 'N/A')}% occupancy, "
            f"ADR is ${adr.get('current', 'N/A')} which is pacing at "
            f"{adr.get('pacePctOfBudget', 'N/A')}% of budget. "
            f"RevPAR stands at ${revpar.get('current', 'N/A')} with "
            f"{revpar.get('vsYOY', 'N/A')}% year-over-year growth. "
            f"You have {arrivals.get('vipCount', 0)} VIP arrivals today "
            f"including {arrivals.get('ambassadorCount', 0)} Ambassador members. "
            f"There are {urgent_count} urgent and {high_count} high-priority "
            f"items requiring your attention."
        ),
        "es-ES": (
            f"Buenos dias, {gm_first_name}. Hoy es {today_date}. "
            f"La ocupacion es del {occupancy.get('current', 'N/A')}%, "
            f"ADR de ${adr.get('current', 'N/A')} al "
            f"{adr.get('pacePctOfBudget', 'N/A')}% del presupuesto. "
            f"RevPAR de ${revpar.get('current', 'N/A')} con "
            f"{revpar.get('vsYOY', 'N/A')}% interanual. "
            f"Hay {arrivals.get('vipCount', 0)} llegadas VIP hoy. "
            f"Tiene {urgent_count} elementos urgentes y {high_count} de alta prioridad."
        ),
        "ja-JP": (
            f"おはようございます、{gm_first_name}様。本日は{today_date}です。"
            f"稼働率は{occupancy.get('current', 'N/A')}%、"
            f"ADRは${adr.get('current', 'N/A')}で予算の"
            f"{adr.get('pacePctOfBudget', 'N/A')}%です。"
            f"RevPARは${revpar.get('current', 'N/A')}、"
            f"前年比{revpar.get('vsYOY', 'N/A')}%増です。"
            f"本日のVIP到着は{arrivals.get('vipCount', 0)}名です。"
            f"緊急{urgent_count}件、重要{high_count}件の対応事項があります。"
        ),
        "zh-CN": (
            f"早上好，{gm_first_name}。今天是{today_date}。"
            f"入住率为{occupancy.get('current', 'N/A')}%，"
            f"ADR为${adr.get('current', 'N/A')}，预算进度"
            f"{adr.get('pacePctOfBudget', 'N/A')}%。"
            f"RevPAR为${revpar.get('current', 'N/A')}，"
            f"同比增长{revpar.get('vsYOY', 'N/A')}%。"
            f"今日有{arrivals.get('vipCount', 0)}位VIP到店。"
            f"有{urgent_count}项紧急和{high_count}项高优先级事项需要关注。"
        ),
    }

    narrative = fallback_templates.get(language, fallback_templates["en-US"])

    logger.info(
        "Fallback narrative generated",
        language=language,
        narrative_length=len(narrative),
    )

    return narrative
