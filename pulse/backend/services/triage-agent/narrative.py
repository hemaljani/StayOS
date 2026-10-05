"""Strands Agent + narrative invoker for the Triage Agent.

The deterministic ``pulse.triage`` code owns the *structural* guarantees; the
model owns the *narrative* (the summary prose and, for complaints, the remedy
options). This module builds the Strands Agent (mirroring the LUMI chat agent:
``BedrockModel`` backed by Claude Sonnet plus the Gateway-discovered tools) and
exposes it to ``pulse.triage.bedrock_client.generate_triage_brief`` through its
``BedrockInvoker`` seam -- a ``Callable[[model_id, prompt], str]`` that returns
the raw model text.

Keeping the model call behind the reused ``generate_triage_brief`` seam means
the narrative flows through the same strict-JSON prompt -> parse -> validate ->
specialize pipeline the former Lambda used, so Property 18 and the per-type
guarantees are enforced identically. Strands is imported lazily so this module
imports cleanly where ``strands`` is absent; the unit tests inject a fake
invoker and never construct a Strands Agent or call Bedrock.
"""

from __future__ import annotations

from typing import Any

from pulse.common.errors import TriageFailure
from pulse.common.logging import get_logger

logger = get_logger("pulse-triage-agent")

# Narrative model settings. Temperature 0 keeps the brief reproducible for a
# given situation; the token budget is small because the model returns compact
# strict JSON (the structure is enforced downstream, not by the model).
MODEL_MAX_TOKENS = 1024
MODEL_TEMPERATURE = 0.0

# System prompt: constrain the model to strict JSON and defer structure to the
# deterministic code. Each per-type template already specifies the exact schema.
_SYSTEM_PROMPT = (
    "You are the PULSE hotel operations triage agent. Using only the situation "
    "facts provided, produce a decision-ready triage brief for the General "
    "Manager. Respond with STRICT JSON only, no prose and no markdown fences, "
    "matching exactly the schema described in the user message. Never invent "
    "facts that are not supported by the situation data."
)


def build_strands_agent(model_id: str, tools: list[Any]) -> Any:
    """Build the per-invocation Strands Agent (Claude Sonnet + Gateway tools).

    Mirrors the LUMI chat agent's construction so the triage runtime stands on
    the same platform pattern: a ``BedrockModel`` and the tools discovered from
    the shared Gateway. ``callback_handler=None`` disables Strands' console
    printer (this is a request-response service, not an interactive stream).

    Args:
        model_id: The Bedrock model id (from ``TRIAGE_MODEL_ID``).
        tools: The Gateway-discovered tools to attach to the agent.

    Returns:
        A constructed Strands ``Agent`` instance.
    """
    from strands import Agent
    from strands.models.bedrock import BedrockModel

    model = BedrockModel(
        model_id=model_id,
        max_tokens=MODEL_MAX_TOKENS,
        temperature=MODEL_TEMPERATURE,
    )
    return Agent(
        model=model,
        tools=tools,
        system_prompt=_SYSTEM_PROMPT,
        callback_handler=None,
    )


def _extract_agent_text(result: Any) -> str:
    """Extract the response text from a Strands ``Agent`` call result.

    Strands returns an ``AgentResult`` whose ``str()`` yields the final message
    text. This reads the text defensively across SDK shapes.

    Args:
        result: The value returned by calling the agent.

    Returns:
        The model's response text.
    """
    # AgentResult.__str__ returns the aggregated text for all recent SDK
    # versions; fall back to a ``message``/``content`` walk if needed.
    text = str(result).strip()
    if text:
        return text
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        for block in message.get("content", []):
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]
    return ""


def make_strands_invoker(agent: Any) -> Any:
    """Adapt a Strands Agent to the ``generate_triage_brief`` invoker seam.

    Args:
        agent: The Strands Agent built by :func:`build_strands_agent`.

    Returns:
        A ``Callable[[str, str], str]`` that runs one narrative turn and returns
        the raw model text. It ignores the ``model_id`` argument because the
        model is already bound into the agent; the signature matches the
        ``BedrockInvoker`` contract.
    """

    def _invoke(_model_id: str, prompt: str) -> str:
        try:
            result = agent(prompt)
        except Exception as error:  # noqa: BLE001 - any model error -> triage failure
            raise TriageFailure(
                f"Triage narrative model invocation failed: {error}",
                reason="bedrock_error",
            ) from error
        return _extract_agent_text(result)

    return _invoke


__all__ = ["build_strands_agent", "make_strands_invoker"]
