// Shared test fixtures for the PULSE PWA test suite (Tasks 21.5-21.7).
//
// Provides a deterministic `makeAlert` factory (fills every required Alert field
// so tests only specify what they care about) and fast-check arbitraries for the
// two property tests (Property 20 resolved-exclusion, Property 23 tier filter).
// Kept free of React so both the pure-logic property tests and the rendering UI
// tests can import it.

import fc from 'fast-check';
import type { TierSelection } from '@/lib/format';
import type { Alert, AlertStatus, AlertTier, AlertType } from '@/lib/types';

// All enum domains, used by both the factory defaults and the arbitraries.
export const ALERT_TIERS: AlertTier[] = ['CRITICAL', 'WARNING', 'INFO'];

export const ALERT_STATUSES: AlertStatus[] = [
  'UNACKNOWLEDGED',
  'ACKNOWLEDGED',
  'RESOLVED',
  'ESCALATED',
  'ESCALATION_EXHAUSTED',
];

export const ALERT_TYPES: AlertType[] = [
  'WALK_RISK',
  'VIP_ROOM_NOT_READY',
  'COMPLAINT_ESCALATION',
  'OOO_CLUSTER',
  'PREMIUM_CANCELLATION',
  'VIP_CHECKIN',
];

// Monotonic counter so default alertId/dedupeKey values are unique per call.
let alertCounter = 0;

// Build a fully-populated Alert, overriding only the fields a test specifies.
export function makeAlert(overrides: Partial<Alert> = {}): Alert {
  alertCounter += 1;
  return {
    alertId: `alert-${alertCounter}`,
    propertyId: 'prop-1',
    tier: 'INFO',
    type: 'VIP_CHECKIN',
    title: `Alert ${alertCounter}`,
    detail: 'Alert detail',
    status: 'UNACKNOWLEDGED',
    createdAt: new Date('2026-01-01T00:00:00.000Z').toISOString(),
    dedupeKey: `dedupe-${alertCounter}`,
    ...overrides,
  };
}

// A single arbitrary Alert with a random tier, status (including RESOLVED), and
// type. alertId is a uuid so generated sets are effectively unique.
export const alertArb: fc.Arbitrary<Alert> = fc
  .record({
    alertId: fc.uuid(),
    tier: fc.constantFrom(...ALERT_TIERS),
    status: fc.constantFrom(...ALERT_STATUSES),
    type: fc.constantFrom(...ALERT_TYPES),
    title: fc.string({ minLength: 1, maxLength: 40 }),
    createdAt: fc
      .date({ min: new Date('2000-01-01T00:00:00.000Z'), max: new Date('2100-01-01T00:00:00.000Z') })
      .map((date) => date.toISOString()),
  })
  .map((fields) => makeAlert(fields));

// An arbitrary list of alerts (0-40) covering empty and mixed-status feeds.
export const alertListArb: fc.Arbitrary<Alert[]> = fc.array(alertArb, { maxLength: 40 });

// The four tier-filter selections the PULSE tab offers.
export const tierSelectionArb: fc.Arbitrary<TierSelection> = fc.constantFrom<TierSelection>(
  'ALL',
  'CRITICAL',
  'WARNING',
  'INFO'
);
