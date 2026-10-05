// KpiGrid - the PULSE tab key-performance-indicator grid (Requirement 15.4).
//
// The PULSE KPI grid summarizes the current alert situation, computed directly
// from the fetched alert feed so it stays self-consistent with the live feed and
// resolved history below it. This differs from the prototype's hotel KPIs
// (occupancy/ADR/RevPAR), which are sourced from LUMI's brief API and are not
// available to PULSE; alert-state KPIs are the meaningful PULSE-native metrics
// and use data PULSE already holds. Cards: Active (non-resolved), Critical
// (active), Warning (active), and Resolved (in the current feed window).

import type { Alert } from '@/lib/types';
import { isLiveAlert } from '@/lib/format';
import KpiCard from './KpiCard';

interface KpiGridProps {
  alerts: Alert[];
}

export default function KpiGrid({ alerts }: KpiGridProps) {
  // Active = everything currently in the live feed (RESOLVED excluded, Property 20).
  const live = alerts.filter(isLiveAlert);
  const activeCount = live.length;
  const criticalCount = live.filter((a) => a.tier === 'CRITICAL').length;
  const warningCount = live.filter((a) => a.tier === 'WARNING').length;
  const resolvedCount = alerts.filter((a) => a.status === 'RESOLVED').length;

  return (
    <div className="grid grid-cols-2 gap-3 mb-3">
      <KpiCard
        label="Active Alerts"
        value={activeCount}
        valueColor="text-ink"
        sublabel="In the live feed"
      />
      <KpiCard
        label="Critical"
        value={criticalCount}
        valueColor="text-tier-critical"
        sublabel="Need attention now"
      />
      <KpiCard
        label="Warning"
        value={warningCount}
        valueColor="text-tier-warning"
        sublabel="Monitor closely"
      />
      <KpiCard
        label="Resolved"
        value={resolvedCount}
        valueColor="text-success"
        sublabel="This shift"
      />
    </div>
  );
}
