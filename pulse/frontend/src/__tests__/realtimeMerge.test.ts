// UI/behavior test for Task 21.7 - realtime feed updates (Requirement 15.4).
//
// The live feed stays current by folding AppSync Events into the alert list via
// mergeEvent (the pure merge in src/hooks/useRealtimeAlerts.ts). This test drives
// that merge directly: an ALERT_CREATED adds a card to the live feed, then an
// ALERT_RESOLVED for the same alert removes it from the live feed and moves it to
// resolved history, with no reload and no duplicate rows.

import { describe, it, expect } from 'vitest';
import { mergeEvent } from '@/hooks/useRealtimeAlerts';
import { partitionAlertsByStatus } from '@/lib/format';
import type { RealtimeAlertEvent } from '@/lib/types';

describe('realtime feed merge (Requirement 15.4)', () => {
  it('adds a card on ALERT_CREATED then moves it to resolved on ALERT_RESOLVED', () => {
    const created: RealtimeAlertEvent = {
      eventType: 'ALERT_CREATED',
      alertId: 'a1',
      propertyId: 'p1',
      tier: 'CRITICAL',
      type: 'WALK_RISK',
      status: 'UNACKNOWLEDGED',
      title: 'Walk Risk detected',
    };

    // ALERT_CREATED: the alert appears in the live feed, not in resolved.
    const afterCreate = mergeEvent([], created);
    expect(afterCreate).toHaveLength(1);
    const createdSplit = partitionAlertsByStatus(afterCreate);
    expect(createdSplit.live.map((alert) => alert.alertId)).toContain('a1');
    expect(createdSplit.resolved).toHaveLength(0);

    const resolved: RealtimeAlertEvent = {
      eventType: 'ALERT_RESOLVED',
      alertId: 'a1',
      propertyId: 'p1',
    };

    // ALERT_RESOLVED: the same alert leaves the live feed and enters resolved
    // history, with no duplicate row created.
    const afterResolve = mergeEvent(afterCreate, resolved);
    expect(afterResolve).toHaveLength(1);
    const resolvedSplit = partitionAlertsByStatus(afterResolve);
    expect(resolvedSplit.live).toHaveLength(0);
    expect(resolvedSplit.resolved.map((alert) => alert.alertId)).toContain('a1');
  });

  it('updates an existing card in place and flips the agent-ready badge on ALERT_UPDATED', () => {
    const seed = mergeEvent([], {
      eventType: 'ALERT_CREATED',
      alertId: 'a2',
      propertyId: 'p1',
      tier: 'WARNING',
      status: 'UNACKNOWLEDGED',
      title: 'VIP room not ready',
    });
    expect(seed[0].triageBrief).toBeNull();

    const updated = mergeEvent(seed, {
      eventType: 'ALERT_UPDATED',
      alertId: 'a2',
      propertyId: 'p1',
      hasTriageBrief: true,
    });

    // Still one row (updated in place), now agent-ready.
    expect(updated).toHaveLength(1);
    expect(updated[0].triageBrief).not.toBeNull();
  });

  it('does not clear an existing triage brief on a later brief-free event (BUG-010)', () => {
    // A card that already carries a brief (e.g. from the REST snapshot or a
    // prior ALERT_UPDATED with hasTriageBrief=true).
    const seed = mergeEvent([], {
      eventType: 'ALERT_CREATED',
      alertId: 'a3',
      propertyId: 'p1',
      tier: 'CRITICAL',
      status: 'UNACKNOWLEDGED',
      title: 'Walk Risk detected',
      hasTriageBrief: true,
    });
    expect(seed[0].triageBrief).not.toBeNull();

    // A subsequent status-change event that does NOT report a brief must not
    // downgrade the card back to "no brief" (the live-feed regression).
    const afterStatusChange = mergeEvent(seed, {
      eventType: 'ALERT_UPDATED',
      alertId: 'a3',
      propertyId: 'p1',
      status: 'ACKNOWLEDGED',
      // hasTriageBrief omitted (falsy)
    });
    expect(afterStatusChange[0].triageBrief).not.toBeNull();
    expect(afterStatusChange[0].status).toBe('ACKNOWLEDGED');
  });
});
