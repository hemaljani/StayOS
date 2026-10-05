// Property test for Task 21.6 - the tier filter shows exactly the matching tier.
//
// Exercises the real filter logic the PULSE tab uses (filterAlertsByTier in
// src/lib/format.ts, applied by page.tsx to the live feed). For any generated set
// of alerts and any selected filter value (ALL/CRITICAL/WARNING/INFO): selecting
// a specific tier yields exactly the live alerts of that tier, and ALL yields
// every live alert. The filter always operates on the live feed, so a resolved
// alert can never appear in the filtered result.

import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { partitionAlertsByStatus, filterAlertsByTier } from '@/lib/format';
import { alertListArb, tierSelectionArb } from './alertFixtures';

describe('Property 23: tier filter shows exactly the matching tier', () => {
  // Feature: initial-pulse-project, Property 23: Tier filter shows exactly the matching tier
  it('yields exactly the live alerts of the selected tier, and every tier for ALL', () => {
    fc.assert(
      fc.property(alertListArb, tierSelectionArb, (alerts, selection) => {
        // The filter is applied to the live feed (resolved already excluded).
        const { live } = partitionAlertsByStatus(alerts);
        const filtered = filterAlertsByTier(live, selection);

        if (selection === 'ALL') {
          // ALL is the identity on the live feed.
          expect(filtered).toEqual(live);
        } else {
          // Every result is of the selected tier...
          expect(filtered.every((alert) => alert.tier === selection)).toBe(true);
          // ...and exactly the matching live alerts are returned (none missed).
          const expectedCount = live.filter((alert) => alert.tier === selection).length;
          expect(filtered.length).toBe(expectedCount);
        }

        // Regardless of selection, the filtered feed never includes a resolved alert.
        expect(filtered.every((alert) => alert.status !== 'RESOLVED')).toBe(true);
      }),
      { numRuns: 200 }
    );
  });
});
