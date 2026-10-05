// TierFilter - the PULSE live-feed tier filter (Requirement 15.4, 15.5).
//
// Offers exactly four values: All, Critical, Warning, Info. Selecting Critical,
// Warning, or Info shows only alerts of that tier; All shows every tier
// (Property 23). Each pill shows the count of matching live alerts. Rendered as
// an accessible single-select group (aria-pressed on each toggle).

import type { AlertTier } from '@/lib/types';
import { TIER_LABELS } from '@/lib/constants';

// The filter selection: a specific tier or the catch-all "ALL".
export type TierFilterValue = 'ALL' | AlertTier;

interface TierFilterProps {
  value: TierFilterValue;
  onChange: (value: TierFilterValue) => void;
  // Count of live alerts per filter value, for the pill badges.
  counts: Record<TierFilterValue, number>;
}

// The four filter options in display order.
const OPTIONS: { value: TierFilterValue; label: string; activeClass: string }[] = [
  { value: 'ALL', label: 'All', activeClass: 'border-gray-500 bg-gray-500/10 text-ink' },
  { value: 'CRITICAL', label: TIER_LABELS.CRITICAL, activeClass: 'border-tier-critical bg-tier-critical/10 text-tier-critical' },
  { value: 'WARNING', label: TIER_LABELS.WARNING, activeClass: 'border-tier-warning bg-tier-warning/10 text-tier-warning' },
  { value: 'INFO', label: TIER_LABELS.INFO, activeClass: 'border-tier-info bg-tier-info/10 text-tier-info' },
];

export default function TierFilter({ value, onChange, counts }: TierFilterProps) {
  return (
    <div className="flex gap-2 overflow-x-auto pb-1" role="group" aria-label="Filter alerts by tier">
      {OPTIONS.map((option) => {
        const isActive = value === option.value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={isActive}
            onClick={() => onChange(option.value)}
            className={`flex items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors ${
              isActive ? option.activeClass : 'border-gray-700 bg-surface text-gray-400'
            }`}
          >
            {option.label}
            <span className="rounded-full bg-white/10 px-1.5 py-0.5 text-[10px] tabular-nums">
              {counts[option.value] ?? 0}
            </span>
          </button>
        );
      })}
    </div>
  );
}
