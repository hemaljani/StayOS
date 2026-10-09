// Regression coverage for property-local VIP times, independent of viewer zone.
import { describe, expect, it } from 'vitest';
import { formatVipArrival } from '@/lib/format';

describe('VIP arrival formatting', () => {
  it.each([
    ['ALOHA-CHI-001', '2026-10-08T19:00:00Z', '2:00 PM'],
    ['ALOHA-MIA-001', '2026-10-08T19:00:00Z', '3:00 PM'],
    ['ALOHA-TYO-001', '2026-10-08T19:00:00Z', '4:00 AM'],
    ['ALOHA-MAD-001', '2026-10-08T19:00:00Z', '9:00 PM'],
    ['ALOHA-BOM-001', '2026-10-08T19:00:00Z', '12:30 AM'],
    ['ALOHA-CHI-001', '2026-01-08T20:00:00Z', '2:00 PM'],
  ])('uses %s property time for %s', (propertyId, iso, expected) => {
    expect(formatVipArrival(iso, propertyId).time).toBe(expected);
  });

  it('retains the local calendar date and DST abbreviation in the detail', () => {
    expect(formatVipArrival('2026-10-08T19:00:00Z', 'ALOHA-TYO-001').detail)
      .toContain('Oct 9, 2026');
    expect(formatVipArrival('2026-10-08T14:00:00-05:00', 'ALOHA-CHI-001').detail)
      .toContain('CDT');
    expect(formatVipArrival('2026-01-08T14:00:00-06:00', 'ALOHA-CHI-001').detail)
      .toContain('CST');
  });

  it('labels UTC explicitly when the property zone is unknown', () => {
    expect(formatVipArrival('2026-10-08T14:00:00-05:00', 'NEW-PROPERTY').time)
      .toBe('7:00 PM UTC');
  });

  it.each([undefined, '', 'invalid', '2026-10-08T14:00:00'])(
    'does not guess a viewer-local arrival time for %s',
    (iso) => {
      expect(formatVipArrival(iso, 'ALOHA-CHI-001').time).toBe('--');
    },
  );
});
