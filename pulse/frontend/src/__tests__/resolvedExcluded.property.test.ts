// Property test for Task 21.5 - resolved alerts are excluded from the live feed.
//
// Exercises the real partition logic the PULSE tab renders from
// (partitionAlertsByStatus in src/lib/format.ts, which page.tsx uses for the
// live feed and resolved history). For any generated set of alerts with random
// tiers and statuses (including RESOLVED), the live feed must contain NO resolved
// alert and every resolved alert must appear only in the resolved-history view;
// the two views are disjoint by status and together account for every alert.

import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { partitionAlertsByStatus, isLiveAlert } from '@/lib/format';
import { alertListArb } from './alertFixtures';

describe('Property 20: resolved alerts excluded from the live feed', () => {
  // Feature: initial-pulse-project, Property 20: Resolved alerts are excluded from the live feed
  it('partitions any feed so live has no RESOLVED alert and resolved has only RESOLVED alerts', () => {
    fc.assert(
      fc.property(alertListArb, (alerts) => {
        const { live, resolved } = partitionAlertsByStatus(alerts);

        // The live feed never contains a resolved alert (the core invariant).
        expect(live.every((alert) => alert.status !== 'RESOLVED')).toBe(true);
        // Every alert in the live feed is genuinely live per the predicate.
        expect(live.every(isLiveAlert)).toBe(true);

        // The resolved history contains only resolved alerts.
        expect(resolved.every((alert) => alert.status === 'RESOLVED')).toBe(true);

        // The two views are disjoint by status and cover the whole feed: their
        // sizes sum to the input, and the counts match the input's status split.
        expect(live.length + resolved.length).toBe(alerts.length);
        const resolvedInInput = alerts.filter((alert) => alert.status === 'RESOLVED').length;
        expect(resolved.length).toBe(resolvedInInput);
        expect(live.length).toBe(alerts.length - resolvedInInput);

        // No alertId appears in both views (strict disjointness).
        const liveIds = new Set(live.map((alert) => alert.alertId));
        expect(resolved.some((alert) => liveIds.has(alert.alertId))).toBe(false);
      }),
      { numRuns: 200 }
    );
  });
});
