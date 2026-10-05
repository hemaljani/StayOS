'use client';

import { useBrief } from '@/hooks/useBrief';
import { useBriefHistory } from '@/hooks/useBriefHistory';
import KpiCard from '@/components/KpiCard';
import TrendChart from '@/components/TrendChart';
import PaceBar from '@/components/PaceBar';
import { SEVERITY_COLORS, SEVERITY_BG_COLORS } from '@/lib/constants';

export default function RevenuePage() {
  const { brief, loading, error } = useBrief();
  const { history, loading: historyLoading } = useBriefHistory();

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading revenue data...</div>;
  }

  if (error || !brief) {
    return <div className="py-8 text-center text-danger">{error || 'Failed to load brief'}</div>;
  }

  const { dailyKPIs, actionItems } = brief;
  const upsellItems = actionItems.filter((item) => item.type === 'UPSELL_OPPORTUNITY');

  // Calculate total upsell potential from action item data
  const totalEligible = upsellItems.reduce(
    (sum, item) => sum + (Number(item.data?.eligibleCount) || 0),
    0
  );
  const avgUpsellValue = upsellItems.reduce(
    (sum, item) => sum + (Number(item.data?.avgUpsellValuePerNight) || 0),
    0
  ) / (upsellItems.length || 1);
  const totalPotentialRevenue = upsellItems.reduce(
    (sum, item) => sum + (Number(item.data?.totalPotentialRevenue) || 0),
    0
  );

  // Compute actual 7-day deltas from history (fall back to API values if < 7 days)
  const day7Ago = history.length >= 7 ? history[0] : null;
  const computedAdrDelta = day7Ago
    ? dailyKPIs.adr.current - day7Ago.dailyKPIs.adr.current
    : dailyKPIs.adr.vsLastWeek;
  const computedOccDelta = day7Ago
    ? dailyKPIs.occupancy.current - day7Ago.dailyKPIs.occupancy.current
    : dailyKPIs.occupancy.vsLastWeek;

  return (
    <div className="py-4">
      <h2 className="text-lg font-semibold mb-3">Revenue Performance</h2>
      <p className="text-[10px] text-gray-500 mb-3">
        Data as of {new Date(dailyKPIs.asOf).toLocaleString()}
      </p>

      {/* KPI Summary Cards */}
      <div className="grid grid-cols-3 gap-2 mb-5">
        <KpiCard
          label="ADR"
          value={dailyKPIs.adr.current}
          unit="$"
          delta={computedAdrDelta}
          deltaLabel=" vs LW"
        />
        <KpiCard
          label="RevPAR"
          value={dailyKPIs.revPAR.current}
          unit="$"
          delta={dailyKPIs.revPAR.vsYOY}
          deltaLabel="% YOY"
        />
        <KpiCard
          label="Occ %"
          value={dailyKPIs.occupancy.current}
          unit="%"
          delta={computedOccDelta}
          deltaLabel="% vs LW"
        />
      </div>

      {/* 7-Day Revenue Trend */}
      <div className="bg-surface rounded-xl p-4 mb-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">7-Day Trend</h3>
        {historyLoading ? (
          <div className="h-[180px] animate-pulse bg-background rounded" />
        ) : (
          <TrendChart data={history} />
        )}
      </div>

      {/* Pace vs Budget Progress Bars */}
      <div className="bg-surface rounded-xl p-4 mb-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Pace vs Budget</h3>
        <PaceBar
          label="ADR"
          percentage={dailyKPIs.adr.pacePctOfBudget}
          value={`$${dailyKPIs.adr.current}`}
        />
        <PaceBar
          label="RevPAR"
          percentage={Math.round((dailyKPIs.revPAR.current / dailyKPIs.revPAR.budget) * 100)}
          value={`$${dailyKPIs.revPAR.current}`}
        />
        <PaceBar
          label="Occupancy"
          percentage={Math.round((dailyKPIs.occupancy.current / dailyKPIs.occupancy.forecast3pm) * 100)}
          value={`${dailyKPIs.occupancy.current}%`}
        />
      </div>

      {/* Segment Mix Breakdown */}
      <div className="bg-surface rounded-xl p-4 mb-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Segment Mix</h3>
        <div className="space-y-2">
          <SegmentRow label="Group" percentage={35} color="bg-accent" />
          <SegmentRow label="Transient" percentage={50} color="bg-accent-secondary" />
          <SegmentRow label="Contract" percentage={15} color="bg-success" />
        </div>
      </div>

      {/* Upsell Pipeline */}
      <div className="bg-surface rounded-xl p-4 mb-4">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Upsell Pipeline</h3>
        {upsellItems.length > 0 ? (
          <>
            <div className="grid grid-cols-3 gap-2 mb-3">
              <div className="text-center">
                <p className="text-lg font-bold text-accent">{totalEligible}</p>
                <p className="text-[10px] text-gray-500">Eligible Arrivals</p>
              </div>
              <div className="text-center">
                <p className="text-lg font-bold text-accent">${Math.round(avgUpsellValue)}</p>
                <p className="text-[10px] text-gray-500">Avg/Night</p>
              </div>
              <div className="text-center">
                <p className="text-lg font-bold text-success">${Math.round(totalPotentialRevenue).toLocaleString()}</p>
                <p className="text-[10px] text-gray-500">Total Potential</p>
              </div>
            </div>
            <div className="space-y-2">
              {upsellItems.map((item) => (
                <div key={item.id} className="flex items-center gap-2 p-2 rounded-lg bg-background">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${SEVERITY_BG_COLORS[item.severity]} ${SEVERITY_COLORS[item.severity]}`}>
                    {item.severity}
                  </span>
                  <span className="text-xs text-gray-300 flex-1 truncate">{item.title}</span>
                </div>
              ))}
            </div>
            <p className="text-[10px] text-gray-500 mt-3 italic">
              Recommend briefing front desk on upsell targets before check-in window.
            </p>
          </>
        ) : (
          <p className="text-xs text-gray-500">No upsell opportunities identified today.</p>
        )}
      </div>
    </div>
  );
}

function SegmentRow({ label, percentage, color }: { label: string; percentage: number; color: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-xs text-gray-400 w-20">{label}</span>
      <div className="flex-1 h-2 bg-background rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${percentage}%` }} />
      </div>
      <span className="text-xs text-gray-300 w-8 text-right">{percentage}%</span>
    </div>
  );
}
