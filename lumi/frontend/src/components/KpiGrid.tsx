import type { DailyKPIs } from '@/lib/types';
import KpiCard from './KpiCard';

interface KpiGridProps {
  kpis: DailyKPIs;
}

export default function KpiGrid({ kpis }: KpiGridProps) {
  return (
    <>
      {/* Primary KPIs - 2x2 grid */}
      <div className="grid grid-cols-2 gap-3 mb-3">
        <KpiCard
          label="Occupancy"
          value={kpis.occupancy.current}
          unit="%"
          delta={kpis.occupancy.vsLastWeek}
          deltaLabel="% vs LW"
          sublabel={`Forecast ${kpis.occupancy.forecast3pm}% by 3 PM`}
        />
        <KpiCard
          label="ADR"
          value={kpis.adr.current}
          unit="$"
          delta={kpis.adr.vsLastWeek}
          deltaLabel=" vs LW"
          sublabel={`${kpis.adr.pacePctOfBudget}% of budget`}
        />
        <KpiCard
          label="RevPAR"
          value={kpis.revPAR.current}
          unit="$"
          delta={kpis.revPAR.vsYOY}
          deltaLabel="% YOY"
          sublabel={`vs $${kpis.revPAR.budget} budget`}
        />
        <KpiCard
          label="Confirmed"
          value={kpis.confirmedReservations}
          sublabel={`of ${kpis.availableRooms} rooms`}
        />
      </div>

      {/* Secondary KPIs - 3-column operational row */}
      <div className="grid grid-cols-3 gap-2 mb-4">
        <div className="bg-surface rounded-xl p-3 text-center">
          <p className="text-xs text-gray-400 mb-0.5">Check-ins</p>
          <p className="text-lg font-bold text-success">{kpis.arrivals.total}</p>
        </div>
        <div className="bg-surface rounded-xl p-3 text-center">
          <p className="text-xs text-gray-400 mb-0.5">Check-outs</p>
          <p className="text-lg font-bold text-warning">{kpis.departures.total}</p>
        </div>
        <div className="bg-surface rounded-xl p-3 text-center">
          <p className="text-xs text-gray-400 mb-0.5">Total Rooms</p>
          <p className="text-lg font-bold text-ink">{kpis.availableRooms}</p>
        </div>
      </div>
    </>
  );
}
