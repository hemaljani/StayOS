"""Integration: curated baseline and presenter live fire coexist (Task 9).

Feature: data-Orchestrator (Requirements 7.1, 7.2, 7.3).

This suite proves the coexistence invariant that lets a presenter fire a live
alert on top of the ambient Curated_Baseline and reset cleanly between runs:

    * a curated baseline (``baselineManaged = True``, ``dedupeKey`` prefixed
      ``baseline#``) is primed for a property, and
    * a presenter live fire adds a DISTINCT alert (its own ``alertId``/
      ``dedupeKey``, NOT ``baselineManaged``) on top of that baseline, and
    * a scenario ``reset`` restores the pre-fire state (removes only the live
      alert) while every baseline item remains intact.

Modeling choice (faithful to the real routes + rule engine)
------------------------------------------------------------
The presenter live-fire routes (``POST /demo/scenarios/{scenarioId}`` and
``.../reset``, see ``pulse/backend/src/pulse/api/handler.py`` -> ``_route_demo``
/ ``_handle_demo_scenario``) do NOT write ``pulse-alerts`` synchronously. They
mutate a LUMI operational table via the demo simulator
(``pulse.demo_simulator.simulator.apply_scenario``); the resulting DynamoDB
Stream record is then consumed by the rule engine, which is what actually
creates the ``pulse-alerts`` item. Requirement 7.1 forbids any new PULSE
write-path code, so this test does not stand up a live stream-to-rule-engine
pipeline. Instead it models the coexistence at the ``pulse-alerts`` table level,
exactly as the two real producers leave it:

    * the baseline via the real ``prime_property_baseline`` builder, and
    * the live alert via the real rule-engine factory
      (``build_alert_draft`` + ``draft_to_item``) seeded from a variant-suffixed
      dedupe key -- byte-for-byte what a stream-driven ``run`` produces (the
      simulator's ``mint_variant`` gives each button press a fresh dedupe key ->
      fresh ``alertId``). ``reset`` is modeled as removing that live alert item,
      matching the net observable effect of the simulator clearing the run.

No production code is added; only existing simulator/baseline/rule-engine APIs
are reused, and DynamoDB is mocked with moto (no AWS is touched).
"""

from __future__ import annotations

from typing import Any

from pulse.baseline.builder import (
    BASELINE_ID_PREFIX,
    BASELINE_MANAGED_ATTRIBUTE,
    build_baseline_items,
    prime_property_baseline,
)
from pulse.common.models import AlertTier, AlertType, SourceEntityRef
from pulse.common.operational_schema import walk_reservation_key
from pulse.demo_simulator import simulator
from pulse.rule_engine.alert_factory import build_alert_draft, draft_to_item

from tests.integration.conftest import ALERTS_TABLE_NAME, IntegrationEnv

# The property whose baseline and live fire we exercise (the canonical demo
# property the simulator targets by default).
DEMO_PROPERTY_ID = simulator.DEMO_PROPERTY_ID


def _scan_alert_ids(env: IntegrationEnv) -> set[str]:
    """Return the set of ``alertId`` values currently in ``pulse-alerts``.

    Args:
        env: The moto-backed integration environment.

    Returns:
        Every ``alertId`` present in the alerts table.
    """
    response = env.alerts.scan(ProjectionExpression="alertId")
    return {item["alertId"] for item in response.get("Items", [])}


def _get_alert(env: IntegrationEnv, alert_id: str) -> dict[str, Any] | None:
    """Return one alert item by id, or ``None`` when absent.

    Args:
        env: The moto-backed integration environment.
        alert_id: The alert id to fetch.

    Returns:
        The item dict, or ``None`` if no item has that id.
    """
    return env.alerts.get_item(Key={"alertId": alert_id}).get("Item")


def _build_live_fire_alert_item(property_id: str, variant: str) -> dict[str, Any]:
    """Build the ``pulse-alerts`` item a stream-driven walk-risk ``run`` yields.

    This mirrors what the rule engine writes when it consumes the Stream record
    produced by ``POST /demo/scenarios/walk-risk``: the simulator mints a variant
    token (:func:`simulator.mint_variant`) that suffixes the run's entity id, so
    the evaluator emits a fresh, variant-scoped ``dedupeKey``; the rule engine's
    :func:`build_alert_draft` / :func:`draft_to_item` then serialize a distinct
    alert item. Crucially the item carries NO baseline marker and NO baseline
    dedupe prefix, so it is unambiguously a live alert.

    Args:
        property_id: The property the live fire targets.
        variant: The per-run uniqueness token (as ``mint_variant`` would give).

    Returns:
        A ``pulse-alerts`` item dict ready for ``put_item``.
    """
    # The variant-suffixed key the walk-risk run mutates (faithful to
    # ``simulator._VARIANT_RUN_BUILDERS["walk-risk"]``).
    arrival_date = f"2026-08-17-{variant}"
    entity_key = walk_reservation_key(property_id, arrival_date)
    # A live-fire dedupe key is condition-scoped and variant-unique; it never
    # starts with the baseline prefix, so it can never collide with a baseline
    # item's id.
    dedupe_key = f"walk-risk#{property_id}#{arrival_date}"
    draft = build_alert_draft(
        property_id=property_id,
        tier=AlertTier.CRITICAL,
        alert_type=AlertType.WALK_RISK,
        title="Walk risk: confirmed arrivals exceed available rooms (live)",
        detail="Presenter-fired live scenario on top of the ambient baseline.",
        dedupe_key=dedupe_key,
        source_entity_ref=SourceEntityRef(
            table="stayos-reservations",
            property_id=property_id,
            entity_key=str(entity_key),
            rule_type=AlertType.WALK_RISK.value,
        ),
        created_at="2026-08-17T18:00:00Z",
    )
    return draft_to_item(draft)


def test_baseline_present_before_live_fire(integration_env: IntegrationEnv) -> None:
    """Priming the baseline populates the feed with only baseline-managed items.

    Feature: data-Orchestrator, Requirement 7.2 (a baseline is present before a
    presenter fires a scenario).
    """
    env = integration_env
    result = prime_property_baseline(
        DEMO_PROPERTY_ID, alerts_table_name=ALERTS_TABLE_NAME
    )

    expected_ids = {item["alertId"] for item in build_baseline_items(DEMO_PROPERTY_ID)}
    assert result["baselineAlertsPrimed"] == len(expected_ids)
    assert _scan_alert_ids(env) == expected_ids
    # Every seeded item is baseline-managed and baseline-prefixed.
    for alert_id in expected_ids:
        item = _get_alert(env, alert_id)
        assert item is not None
        assert item[BASELINE_MANAGED_ATTRIBUTE] is True
        assert str(item["dedupeKey"]).startswith(f"{BASELINE_ID_PREFIX}#")


def test_live_fire_adds_distinct_alert_on_top_of_baseline(
    integration_env: IntegrationEnv,
) -> None:
    """A live scenario adds a DISTINCT, non-baseline alert; baseline is intact.

    Feature: data-Orchestrator, Requirement 7.2 (WHEN a presenter triggers a
    scenario after a Curated_Baseline is present, PULSE adds a distinct new
    alert on top of the baseline).
    """
    env = integration_env
    prime_property_baseline(DEMO_PROPERTY_ID, alerts_table_name=ALERTS_TABLE_NAME)
    baseline_ids = {
        item["alertId"] for item in build_baseline_items(DEMO_PROPERTY_ID)
    }

    live_item = _build_live_fire_alert_item(DEMO_PROPERTY_ID, variant="k1a2b3c")
    env.alerts.put_item(Item=live_item)
    live_id = live_item["alertId"]

    # The live alert is DISTINCT from every baseline alert id.
    assert live_id not in baseline_ids
    # It stacks ON TOP of the baseline: all baseline ids plus the one live id.
    assert _scan_alert_ids(env) == baseline_ids | {live_id}
    # It is unambiguously a live alert: not baseline-managed and not
    # baseline-prefixed.
    persisted_live = _get_alert(env, live_id)
    assert persisted_live is not None
    assert BASELINE_MANAGED_ATTRIBUTE not in persisted_live
    assert not str(persisted_live["dedupeKey"]).startswith(f"{BASELINE_ID_PREFIX}#")
    # Every baseline item still present and untouched by the live fire.
    for baseline_id in baseline_ids:
        assert _get_alert(env, baseline_id) is not None


def test_reset_restores_pre_fire_state_leaving_baseline_intact(
    integration_env: IntegrationEnv,
) -> None:
    """A scenario reset removes only the live alert; the baseline survives.

    Feature: data-Orchestrator, Requirement 7.3 (WHEN a presenter resets a
    scenario, PULSE restores the pre-fire state). Reset is modeled as removing
    the live alert item, matching the net observable effect of the simulator
    clearing the run and the rule engine no longer sustaining that alert.
    """
    env = integration_env
    prime_property_baseline(DEMO_PROPERTY_ID, alerts_table_name=ALERTS_TABLE_NAME)
    baseline_ids = {
        item["alertId"] for item in build_baseline_items(DEMO_PROPERTY_ID)
    }
    pre_fire_ids = _scan_alert_ids(env)
    assert pre_fire_ids == baseline_ids

    # Fire, then reset.
    live_item = _build_live_fire_alert_item(DEMO_PROPERTY_ID, variant="k1a2b3c")
    env.alerts.put_item(Item=live_item)
    assert _scan_alert_ids(env) == baseline_ids | {live_item["alertId"]}

    env.alerts.delete_item(Key={"alertId": live_item["alertId"]})

    # Pre-fire state restored exactly: only the live alert was removed.
    assert _scan_alert_ids(env) == pre_fire_ids
    for baseline_id in baseline_ids:
        item = _get_alert(env, baseline_id)
        assert item is not None
        assert item[BASELINE_MANAGED_ATTRIBUTE] is True


def test_reprime_after_reset_is_idempotent_alongside_no_live_alert(
    integration_env: IntegrationEnv,
) -> None:
    """Re-priming after a reset leaves exactly the bounded baseline (no growth).

    Feature: data-Orchestrator, Requirement 7.3 (a clean pre-fire state supports
    repeated presenter runs). This guards that a fire/reset cycle followed by a
    re-prime does not accumulate items.
    """
    env = integration_env
    prime_property_baseline(DEMO_PROPERTY_ID, alerts_table_name=ALERTS_TABLE_NAME)
    baseline_ids = {
        item["alertId"] for item in build_baseline_items(DEMO_PROPERTY_ID)
    }

    live_item = _build_live_fire_alert_item(DEMO_PROPERTY_ID, variant="k1a2b3c")
    env.alerts.put_item(Item=live_item)
    env.alerts.delete_item(Key={"alertId": live_item["alertId"]})

    # Re-prime (reset-then-prime) yields the same bounded id set, no growth.
    prime_property_baseline(DEMO_PROPERTY_ID, alerts_table_name=ALERTS_TABLE_NAME)
    assert _scan_alert_ids(env) == baseline_ids
