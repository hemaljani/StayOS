// Ops tab - facility summary, OOO rooms, and group checkout (Requirement 15.11).
//
// Fetches GET /ops (via useOps) and renders: a facility KPI grid (occupancy,
// arrivals/departures, OOO count, open work orders), the out-of-order room cards
// (each with its joined work-order status via OpsRoomCard), and a group-checkout
// summary. Loading, empty, and error states mirror the PULSE tab.

'use client';

import { Wrench } from 'lucide-react';
import { useOps } from '@/hooks/useOps';
import KpiCard from '@/components/KpiCard';
import OpsRoomCard from '@/components/OpsRoomCard';

export default function OpsPage() {
  const { data, loading, error, refetch } = useOps();

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading operations...</div>;
  }

  if (error) {
    return (
      <div className="py-8 text-center">
        <p className="text-danger mb-3">{error}</p>
        <button type="button" onClick={() => refetch()} className="text-sm text-accent underline">
          Try again
        </button>
      </div>
    );
  }

  const facility = data?.facility;
  const oooRooms = data?.oooRooms ?? [];
  const groupCheckout = data?.groupCheckout;

  return (
    <div className="py-4">
      <h2 className="text-lg font-semibold mb-4">Operations</h2>

      {/* Facility summary KPIs */}
      {facility && (
        <div className="grid grid-cols-2 gap-3 mb-5">
          <KpiCard label="Occupancy" value={`${facility.occupancyPct}%`} sublabel="Current" />
          <KpiCard
            label="Arrivals / Departures"
            value={`${facility.arrivalsTotal} / ${facility.departuresTotal}`}
            sublabel="Today"
          />
          <KpiCard
            label="Rooms OOO"
            value={facility.oooCount}
            valueColor="text-tier-critical"
            sublabel="Out of order"
          />
          <KpiCard
            label="Open Work Orders"
            value={facility.openWorkOrders}
            valueColor="text-tier-warning"
            sublabel="In progress"
          />
        </div>
      )}

      {/* Out-of-order rooms */}
      <section aria-label="Rooms out of order" className="mb-5">
        <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400 mb-2">
          Rooms Out of Order
        </h3>
        {oooRooms.length > 0 ? (
          <div className="space-y-2">
            {oooRooms.map((room, index) => (
              <OpsRoomCard key={room.roomNumber ?? `ooo-${index}`} room={room} />
            ))}
          </div>
        ) : (
          <div className="bg-surface rounded-xl p-6 text-center border border-gray-800">
            <Wrench size={24} className="mx-auto text-success mb-2" strokeWidth={1.5} aria-hidden />
            <p className="text-sm text-gray-400">No rooms out of order.</p>
          </div>
        )}
      </section>

      {/* Group checkout summary */}
      {groupCheckout && (
        <section aria-label="Group checkout" className="mb-2">
          <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400 mb-2">
            Group Checkout &middot; Today
          </h3>
          <div className="bg-surface rounded-xl border border-gray-800 p-4 grid grid-cols-3 gap-2 text-center">
            <div>
              <p className="text-xl font-bold text-ink tabular-nums">{groupCheckout.departuresTotal}</p>
              <p className="text-[9px] uppercase tracking-wide text-gray-500 mt-0.5">Departures</p>
            </div>
            <div>
              <p className="text-xl font-bold text-ink tabular-nums">{groupCheckout.availableRooms}</p>
              <p className="text-[9px] uppercase tracking-wide text-gray-500 mt-0.5">Available</p>
            </div>
            <div>
              <p className="text-xl font-bold text-ink tabular-nums">{groupCheckout.confirmedReservations}</p>
              <p className="text-[9px] uppercase tracking-wide text-gray-500 mt-0.5">Confirmed</p>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
