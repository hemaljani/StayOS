"""Regression checks for required Data Orchestrator deployment wiring."""

from pathlib import Path
import re


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "data-orchestrator.yaml"
).read_text(encoding="utf-8")


def _resource_block(name: str) -> str:
    start = TEMPLATE.index(f"  {name}:")
    body_start = TEMPLATE.index("\n", start) + 1
    match = re.search(
        r"(?m)^  [A-Za-z][A-Za-z0-9]*:\s*$",
        TEMPLATE[body_start:],
    )
    end = len(TEMPLATE) if match is None else body_start + match.start()
    return TEMPLATE[start:end]


def test_prime_baseline_receives_pulse_alerts_table() -> None:
    """The deployed handler must not silently degrade to a zero-item no-op."""
    assert "PulseAlertsTableName:" in TEMPLATE
    prime_block = _resource_block("PrimeBaselineFunction")
    assert "ALERTS_TABLE_NAME: !Ref PulseAlertsTableName" in prime_block
    assert "ALERTS_TABLE_NAME" not in _resource_block("QuiesceFunction")


def test_prime_baseline_role_can_write_only_the_alerts_table() -> None:
    """The bounded reset-and-prime cycle requires BatchWriteItem."""
    assert "PolicyName: PrimeBaselineAccess" in TEMPLATE
    assert '"dynamodb:BatchWriteItem"' in TEMPLATE
    assert "table/${PulseAlertsTableName}" in TEMPLATE
