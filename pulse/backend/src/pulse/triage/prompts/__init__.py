"""Per-alert-type Bedrock prompt templates and rendering for the Triage Agent.

Each triage-eligible alert type (CRITICAL/WARNING) has a strict-JSON prompt
template stored as a ``.txt`` file in this package. The template instructs the
model to return only JSON matching the ``triageBrief`` schema, so the output can
be parsed and validated by :mod:`pulse.triage.validation`. Templates are loaded
via :mod:`importlib.resources` so they ship inside the Lambda package regardless
of the working directory.

Each template contains a single ``__CONTEXT__`` placeholder into which
:func:`render_prompt` injects the JSON-serialized, non-sensitive situation
facts. A plain-text placeholder (rather than ``str.format``) is used so the
JSON braces in the templates are never mistaken for format fields.
"""

from __future__ import annotations

import json
from dataclasses import asdict

# PULSE backend targets Python 3.12 only (pyproject: requires-python >=3.12), so
# importlib.resources is standard and preferred here (it ships templates inside
# the Lambda package); the importlib_resources backport is unnecessary. The
# nosemgrep directive is on the import line itself so the suppression applies.
from importlib import resources  # nosemgrep: python37-compatibility-importlib2
from typing import Any

from pulse.common.errors import TriageFailure
from pulse.common.models import AlertType
from pulse.triage.context import SituationContext

# Placeholder token replaced with the serialized situation facts at render time.
_CONTEXT_TOKEN = "__CONTEXT__"

# Map each triage-eligible alert type to its template file. The CRITICAL/WARNING
# types produce action briefs; the two INFO types (premium cancellation, VIP
# check-in) produce advisory "opportunity" briefs (no executable write-back) so
# GMs get ranked suggestions rather than a bare notification.
_TEMPLATE_FILES: dict[AlertType, str] = {
    AlertType.WALK_RISK: "walk_risk.txt",
    AlertType.VIP_ROOM_NOT_READY: "vip_room_not_ready.txt",
    AlertType.COMPLAINT_ESCALATION: "complaint_escalation.txt",
    AlertType.OOO_CLUSTER: "ooo_cluster.txt",
    AlertType.PREMIUM_CANCELLATION: "premium_cancellation.txt",
    AlertType.VIP_CHECKIN: "vip_checkin.txt",
}


def _load_template(alert_type: AlertType) -> str:
    """Load the raw prompt template text for an alert type.

    Args:
        alert_type: The triage-eligible alert type.

    Returns:
        The template file contents.

    Raises:
        TriageFailure: If the alert type has no prompt template (an alert type
            not present in ``_TEMPLATE_FILES``).
    """
    filename = _TEMPLATE_FILES.get(alert_type)
    if filename is None:
        raise TriageFailure(
            f"No triage prompt template for alert type {alert_type.value}",
            reason="unsupported_type",
        )
    return resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")


def _context_payload(context: SituationContext) -> dict[str, Any]:
    """Build a JSON-safe view of the situation context for prompt injection.

    Excludes the non-serializable lookup seams and any callable, leaving only
    the factual fields the model should reason over.

    Args:
        context: The situation context.

    Returns:
        A JSON-serializable dict of the context's factual fields.
    """
    payload = asdict(context)
    # Drop non-serializable / seam fields.
    payload.pop("sister_property_lookup", None)
    return {key: value for key, value in payload.items() if not callable(value)}


def render_prompt(alert_type: AlertType, context: SituationContext) -> str:
    """Render the strict-JSON triage prompt for an alert type and context.

    Args:
        alert_type: The triage-eligible alert type whose template to render.
        context: The situation facts to inject.

    Returns:
        The fully-rendered prompt string ready to send to Bedrock.

    Raises:
        TriageFailure: If the alert type has no prompt template.
    """
    template = _load_template(alert_type)
    facts = json.dumps(_context_payload(context), default=str, indent=2)
    return template.replace(_CONTEXT_TOKEN, facts)


__all__ = ["render_prompt"]
