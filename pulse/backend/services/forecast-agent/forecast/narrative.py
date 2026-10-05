"""Remediation Plan domain model for the PULSE Forecasting Agent.

This module defines :class:`RemediationPlan`, the pre-staged walk plan attached
to a predictive oversell alert. The record carries two clearly separated parts,
which is the central determinism boundary of the feature:

- **Deterministic numeric facts** (``rooms_oversold``, ``anticipated_date``,
  ``confidence``, and the overflow option) come from the pure Forecast Engine
  (``engine.OversellPrediction``) and are never model-generated (Req 3.9).
- **AI-authored content** (``narrative`` and ``steps``) is authored by the
  Narrative_Model (Claude Sonnet 4.6 via the Bedrock Converse API) around those
  fixed numbers, which it must reproduce and never recompute (Req 3.7, 3.8). The
  ``authored`` marker records whether the content came from the model
  (``"MODEL"``) or a deterministic template fallback (``"TEMPLATE"``), so an
  alert is never blocked on the model (Req 3.10).

Only the frozen dataclass definition lives here for now. The authoring function
``author_remediation_plan`` (Bedrock Converse call plus deterministic template
fallback) is implemented in a later task (7.1).

Fields use snake_case (NAMING-03) and carry full type hints (PYQUALITY-01).

Requirements: 3.4, 3.7, 3.8, 3.9, 3.10.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RemediationPlan:
    """A pre-staged, advisory walk plan for a predicted oversell night.

    Combines the pure engine's deterministic numeric facts with the
    Narrative_Model's authored prose. The numeric facts are supplied by the
    engine and MUST match ``engine.OversellPrediction`` (Req 3.9); the model
    authors only ``narrative`` and ``steps`` and may never alter the numbers
    (Req 3.7, 3.8). The plan is a reviewable proposal and is never marked
    executed or applied (Req 5.3).

    Attributes:
        rooms_oversold: Forecasted demand minus sellable capacity, >= 1;
            deterministic, from the pure engine, not model-generated (Req 3.7,
            3.9).
        anticipated_date: The oversold local calendar day, as an ISO
            ``YYYY-MM-DD`` string; deterministic (Req 3.7, 3.9).
        confidence: The prediction confidence as an integer in [0, 100];
            deterministic (Req 3.4, 3.9).
        overflow_sister_property_id: The deterministic overflow candidate sister
            property id, present only when sister availability was retrieved for
            overflow evaluation (Req 2.3, 6.6). ``None`` otherwise.
        overflow_available_rooms: The deterministic available-room count at the
            overflow candidate, present only when sister availability was
            retrieved (Req 2.3, 6.6). ``None`` otherwise.
        narrative: The AI-authored plan prose; empty on construction until
            authored. On model failure a deterministic template narrative is
            used instead (Req 3.8, 3.10).
        steps: The AI-authored walk-plan / mitigation steps; empty on
            construction until authored. Deterministic template steps are used
            on model failure (Req 3.8, 3.10).
        authored: The authoring marker, ``"MODEL"`` when the Narrative_Model
            authored the content or ``"TEMPLATE"`` on the deterministic fallback
            (Req 3.10). Defaults to ``"TEMPLATE"``.
    """

    # Deterministic numeric facts (from the pure engine; not model-generated,
    # Req 3.9).
    rooms_oversold: int
    anticipated_date: str
    confidence: int
    overflow_sister_property_id: Optional[str] = None
    overflow_available_rooms: Optional[int] = None
    # AI-authored content (Narrative_Model; deterministic template on fallback,
    # Req 3.10). Defaults keep the record constructible before authoring.
    narrative: str = ""
    steps: tuple[str, ...] = field(default_factory=tuple)
    authored: str = "TEMPLATE"


# ---------------------------------------------------------------------------
# Remediation Plan authoring (task 7.1, Req 3.7, 3.8, 3.9, 3.10)
# ---------------------------------------------------------------------------
#
# The determinism boundary: the pure engine owns every NUMBER (rooms_oversold,
# anticipated_date, confidence, and the overflow option). The Narrative_Model
# (Claude Sonnet 4.6 via the Bedrock Converse API) authors ONLY the prose and
# the walk-plan steps, around those numbers, which it must reproduce and never
# recompute (Req 3.7, 3.8). On any model failure -- or when no model is
# configured -- a deterministic template carrying the IDENTICAL numbers is used
# instead, so a plan is NEVER blocked on the model (Req 3.10).

import json
import logging
from typing import Any, Callable

try:  # Prefer Powertools structured logging when the layer is present.
    from aws_lambda_powertools import Logger

    logger: Any = Logger(service="pulse-forecaster")
except ImportError:  # Fall back to the stdlib logger where Powertools is absent.
    logger = logging.getLogger(__name__)

# A model invoker takes (model_id, prompt) and returns the raw model text. It is
# injectable so tests can supply present/absent/failing models WITHOUT calling
# Bedrock; ``None`` selects the default Converse-based invoker (Req 3.10).
ModelInvoker = Callable[[str, str], str]

# Narrative model settings. Temperature 0 keeps the authored plan reproducible
# for a given oversell; the token budget is modest because the model returns a
# compact strict-JSON object with only the prose and the steps.
MODEL_MAX_TOKENS = 1024
MODEL_TEMPERATURE = 0.0

# System prompt: constrain the model to strict JSON authoring the narrative and
# steps ONLY, and forbid it from altering the fixed numeric facts (Req 3.8).
_SYSTEM_PROMPT = (
    "You are the PULSE hotel Remediation Plan Author. You are given the FIXED, "
    "already-computed facts of a predicted oversell night. You must NOT change, "
    "recompute, or contradict any number. Author a concise, decision-ready walk "
    "plan for the General Manager that reasons around those exact facts. Respond "
    "with STRICT JSON only, no prose and no markdown fences, matching exactly: "
    '{"narrative": "<string>", "steps": ["<string>", ...]}. Reproduce the given '
    "numbers faithfully; never invent facts beyond the supplied ones."
)


class RemediationAuthoringError(Exception):
    """Raised internally when the Narrative_Model output cannot be used.

    Signals a model-side problem (invocation error, unexpected response shape, or
    unparseable JSON) so :func:`author_remediation_plan` can fall back to the
    deterministic template rather than propagating (Req 3.10). It is caught within
    this module and never surfaces to callers -- a plan is never blocked on the
    model.
    """


def _build_author_prompt(
    *,
    rooms_oversold: int,
    anticipated_date: str,
    confidence: int,
    overflow_sister_property_id: Optional[str],
    overflow_available_rooms: Optional[int],
) -> str:
    """Render the user prompt carrying the fixed oversell facts for the model.

    The prompt passes every deterministic number as a FIXED FACT the model must
    reproduce and reason around, never recompute (Req 3.7, 3.8). The overflow
    option is included only when a sister candidate was retrieved.

    Args:
        rooms_oversold: Deterministic forecasted shortfall (>= 1), from the engine.
        anticipated_date: Deterministic oversold day as ISO ``YYYY-MM-DD``.
        confidence: Deterministic confidence integer in [0, 100].
        overflow_sister_property_id: Deterministic overflow candidate id, or
            ``None`` when no sister availability was retrieved.
        overflow_available_rooms: Deterministic available rooms at the candidate,
            or ``None`` when no sister availability was retrieved.

    Returns:
        The rendered prompt string embedding the fixed facts as JSON.
    """
    # Serialize the fixed facts as JSON so the model receives them unambiguously.
    # These values are authoritative; the model reflects them, it never edits them.
    facts: dict[str, Any] = {
        "rooms_oversold": rooms_oversold,
        "anticipated_date": anticipated_date,
        "confidence": confidence,
    }
    if overflow_sister_property_id is not None:
        facts["overflow_sister_property_id"] = overflow_sister_property_id
    if overflow_available_rooms is not None:
        facts["overflow_available_rooms"] = overflow_available_rooms

    return (
        "These are the FIXED facts of a predicted oversell. Do not change any "
        f"number. Author the walk plan around them.\nFIXED_FACTS = "
        f"{json.dumps(facts, sort_keys=True)}\n"
        'Respond with STRICT JSON: {"narrative": "<string>", "steps": '
        '["<string>", ...]}.'
    )


def _default_model_invoker(model_id: str, prompt: str) -> str:
    """Invoke Bedrock via the Converse API and return the model's text output.

    Mirrors the Triage Agent's Converse invoker (design Decision 5): the
    model-agnostic Converse API lets the approved model id be swapped without code
    changes, and temperature 0 keeps the authored plan reproducible. The
    ``bedrock-runtime`` client is created once at module scope (below) with an
    explicit adaptive-retry ``Config`` (PYQUALITY-06); it is not created here on
    each call.

    Args:
        model_id: The Bedrock model id to invoke (from ``FORECAST_MODEL_ID``).
        prompt: The rendered prompt carrying the fixed oversell facts.

    Returns:
        The raw text content of the model's response.

    Raises:
        RemediationAuthoringError: If Bedrock returns an error or an unexpected
            response shape.
    """
    client = _get_bedrock_client()
    try:
        # Converse: a single user turn with a system prompt, low temperature, and
        # a bounded token budget so the model returns compact strict JSON.
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": _SYSTEM_PROMPT}],
            inferenceConfig={
                "maxTokens": MODEL_MAX_TOKENS,
                "temperature": MODEL_TEMPERATURE,
            },
        )
    except client.exceptions.ClientError as exc:  # pragma: no cover - live path
        # Typed boto3 exception (PYQUALITY-02); converted so the caller can fall
        # back to the deterministic template without a Bedrock dependency leaking.
        raise RemediationAuthoringError(
            f"Bedrock Converse invocation failed: {exc}"
        ) from exc

    try:
        return response["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - live path
        raise RemediationAuthoringError(
            "Bedrock Converse response had an unexpected shape"
        ) from exc


def _get_bedrock_client() -> Any:
    """Return the module-level cached ``bedrock-runtime`` client.

    The client is created once and reused across warm invocations for connection
    reuse (PYQUALITY-06). boto3/botocore are imported lazily so this module still
    imports cleanly on hosts without the SDK installed (the unit tests inject a
    fake invoker and never construct a client or call Bedrock).

    Returns:
        A configured, cached boto3 ``bedrock-runtime`` client.
    """
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        import boto3
        from botocore.config import Config

        # Standard retry mode with explicit timeouts, per PYQUALITY-06; the
        # region resolves from the SDK's default chain (never hardcoded).
        config = Config(
            retries={"mode": "standard", "max_attempts": 3},
            connect_timeout=5,
            read_timeout=30,
        )
        _BEDROCK_CLIENT = boto3.client("bedrock-runtime", config=config)
    return _BEDROCK_CLIENT


# Module-level client cache (lazily populated by ``_get_bedrock_client``). Kept as
# a leading-underscore module cache per NAMING-03.
_BEDROCK_CLIENT: Any = None


def _parse_authored_content(raw_text: str) -> tuple[str, tuple[str, ...]]:
    """Parse the model's strict-JSON response into narrative and steps.

    The model authors ONLY the prose and the walk-plan steps; this extracts them
    and coerces the steps to a tuple of strings. Any structural problem (bad JSON,
    missing/empty narrative, non-list steps) raises so the caller falls back to
    the deterministic template (Req 3.10).

    Args:
        raw_text: The raw model response text (expected to be a strict-JSON
            object with ``narrative`` and ``steps``).

    Returns:
        A ``(narrative, steps)`` tuple: the authored prose and the ordered steps.

    Raises:
        RemediationAuthoringError: If the text is not valid JSON, lacks a
            non-empty ``narrative``, or does not carry a list of step strings.
    """
    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RemediationAuthoringError(
            "Narrative_Model response was not valid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise RemediationAuthoringError(
            "Narrative_Model response was not a JSON object"
        )

    narrative = payload.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        raise RemediationAuthoringError(
            "Narrative_Model response was missing a non-empty 'narrative'"
        )

    raw_steps = payload.get("steps", [])
    if not isinstance(raw_steps, list) or not all(
        isinstance(step, str) for step in raw_steps
    ):
        raise RemediationAuthoringError(
            "Narrative_Model 'steps' must be a list of strings"
        )

    return narrative.strip(), tuple(step.strip() for step in raw_steps if step.strip())


def _build_template_plan(
    *,
    rooms_oversold: int,
    anticipated_date: str,
    confidence: int,
    overflow_sister_property_id: Optional[str],
    overflow_available_rooms: Optional[int],
) -> RemediationPlan:
    """Build the deterministic TEMPLATE fallback plan (Req 3.10).

    Produces a fixed, model-free walk plan carrying the IDENTICAL numbers so a
    plan is always available even when the Narrative_Model is unconfigured or
    fails. The prose and steps are derived purely from the inputs (no I/O, no
    model), and ``authored`` is left at its ``"TEMPLATE"`` marker.

    Args:
        rooms_oversold: Deterministic forecasted shortfall (>= 1).
        anticipated_date: Deterministic oversold day as ISO ``YYYY-MM-DD``.
        confidence: Deterministic confidence integer in [0, 100].
        overflow_sister_property_id: Deterministic overflow candidate id, or
            ``None``.
        overflow_available_rooms: Deterministic available rooms at the candidate,
            or ``None``.

    Returns:
        A :class:`RemediationPlan` with ``authored="TEMPLATE"`` carrying the
        deterministic numbers and template prose/steps.
    """
    # Room-vs-rooms wording keeps the single-room case grammatical.
    room_word = "room" if rooms_oversold == 1 else "rooms"
    narrative = (
        f"Predicted oversell of {rooms_oversold} {room_word} on "
        f"{anticipated_date} (confidence {confidence}%). Review and pre-stage a "
        "walk plan; this is an advisory proposal, not an executed action."
    )

    steps: list[str] = [
        f"Confirm the forecasted shortfall of {rooms_oversold} {room_word} for "
        f"{anticipated_date}.",
        "Identify guests eligible to walk (loyalty tier, length of stay, arrival "
        "time).",
    ]
    # Only surface the overflow step when a deterministic candidate exists.
    if overflow_sister_property_id is not None and overflow_available_rooms is not None:
        steps.append(
            f"Hold {overflow_available_rooms} overflow room(s) at sister property "
            f"{overflow_sister_property_id} as the walk destination."
        )
    steps.append(
        "Brief the front desk and prepare walk compensation before the arrival "
        "wave."
    )

    return RemediationPlan(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        overflow_sister_property_id=overflow_sister_property_id,
        overflow_available_rooms=overflow_available_rooms,
        narrative=narrative,
        steps=tuple(steps),
        authored="TEMPLATE",
    )


def author_remediation_plan(
    *,
    rooms_oversold: int,
    anticipated_date: str,
    confidence: int,
    overflow_sister_property_id: Optional[str] = None,
    overflow_available_rooms: Optional[int] = None,
    model_id: Optional[str] = None,
    invoke_model: Optional[ModelInvoker] = None,
) -> RemediationPlan:
    """Author a Remediation_Plan around fixed oversell facts (Req 3.7 - 3.10).

    The determinism boundary is absolute: the returned plan's numeric fields
    (``rooms_oversold``, ``anticipated_date``, ``confidence``, and the overflow
    option) ALWAYS equal the inputs -- they are set from the arguments, never from
    the model output, so the Narrative_Model can never change them (Req 3.9). The
    model authors ONLY ``narrative`` and ``steps``, reasoning around those fixed
    numbers (Req 3.7, 3.8).

    Model selection and the fallback (Req 3.10):

    * When ``invoke_model`` is provided, it is used as the model seam (tests
      inject present/absent/failing models this way without touching Bedrock).
    * When ``invoke_model`` is ``None`` and a ``model_id`` is configured, a default
      Bedrock Converse invoker is built (module-level ``bedrock-runtime`` client,
      adaptive/standard-retry ``Config``).
    * When no model is configured (both ``model_id`` and ``invoke_model`` are
      ``None``), OR the model raises / times out / returns unusable output, a
      deterministic TEMPLATE plan carrying the IDENTICAL numbers is returned. A
      plan is NEVER blocked on the model.

    Args:
        rooms_oversold: Deterministic forecasted shortfall (>= 1), from the pure
            engine; reproduced by the model, never recomputed.
        anticipated_date: Deterministic oversold day as an ISO ``YYYY-MM-DD``
            string, from the engine.
        confidence: Deterministic confidence integer in [0, 100], from the engine.
        overflow_sister_property_id: Optional deterministic overflow candidate id,
            present only when sister availability was retrieved. Defaults to
            ``None``.
        overflow_available_rooms: Optional deterministic available-room count at
            the overflow candidate. Defaults to ``None``.
        model_id: The Bedrock model id (from ``FORECAST_MODEL_ID`` config; never
            hardcoded). ``None`` means no model is configured. Defaults to ``None``.
        invoke_model: Optional injectable model seam ``(model_id, prompt) -> str``
            returning the raw model text. ``None`` selects the default Converse
            invoker when a ``model_id`` is configured. Defaults to ``None``.

    Returns:
        A :class:`RemediationPlan` carrying the input numbers unchanged, with
        model-authored ``narrative``/``steps`` and ``authored="MODEL"`` on success,
        or template prose/steps and ``authored="TEMPLATE"`` on fallback.
    """
    # Choose the invoker seam: explicit injection wins; otherwise build the
    # default Converse invoker only when a model id is actually configured.
    invoker: Optional[ModelInvoker] = invoke_model
    if invoker is None and model_id:
        invoker = _default_model_invoker

    # No model configured at all -> deterministic template (Req 3.10). Never
    # blocked on the model.
    if invoker is None or not model_id:
        if invoke_model is not None and not model_id:
            # An invoker was injected but no model id: nothing to invoke with.
            logger.info(
                "No model_id configured; using deterministic template plan",
                extra={"anticipated_date": anticipated_date},
            )
        return _build_template_plan(
            rooms_oversold=rooms_oversold,
            anticipated_date=anticipated_date,
            confidence=confidence,
            overflow_sister_property_id=overflow_sister_property_id,
            overflow_available_rooms=overflow_available_rooms,
        )

    prompt = _build_author_prompt(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        overflow_sister_property_id=overflow_sister_property_id,
        overflow_available_rooms=overflow_available_rooms,
    )

    try:
        raw_text = invoker(model_id, prompt)
        narrative, steps = _parse_authored_content(raw_text)
    except RemediationAuthoringError as exc:
        # Known model-side problem (invocation, shape, or JSON): log and fall back
        # to the deterministic template. Do NOT propagate (Req 3.10).
        logger.warning(
            "Remediation authoring failed; using deterministic template plan",
            extra={"reason": str(exc), "anticipated_date": anticipated_date},
        )
        return _build_template_plan(
            rooms_oversold=rooms_oversold,
            anticipated_date=anticipated_date,
            confidence=confidence,
            overflow_sister_property_id=overflow_sister_property_id,
            overflow_available_rooms=overflow_available_rooms,
        )
    except Exception as exc:  # noqa: BLE001 - any model error -> template fallback
        # Any other injected-model or transport error (e.g. timeout) must not
        # block the plan; log with context and fall back (Req 3.10, PYQUALITY-03).
        logger.warning(
            "Unexpected error authoring remediation plan; using template",
            extra={"error": repr(exc), "anticipated_date": anticipated_date},
        )
        return _build_template_plan(
            rooms_oversold=rooms_oversold,
            anticipated_date=anticipated_date,
            confidence=confidence,
            overflow_sister_property_id=overflow_sister_property_id,
            overflow_available_rooms=overflow_available_rooms,
        )

    # Success: the model authored the prose/steps. The numeric fields are set from
    # the INPUTS (never the model output), enforcing the determinism boundary
    # (Req 3.9), and the plan is marked MODEL-authored (Req 3.10).
    logger.info(
        "Authored remediation plan via Narrative_Model",
        extra={"anticipated_date": anticipated_date, "authored": "MODEL"},
    )
    return RemediationPlan(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        overflow_sister_property_id=overflow_sister_property_id,
        overflow_available_rooms=overflow_available_rooms,
        narrative=narrative,
        steps=steps,
        authored="MODEL",
    )


__all__ = [
    "RemediationPlan",
    "RemediationAuthoringError",
    "ModelInvoker",
    "author_remediation_plan",
]
