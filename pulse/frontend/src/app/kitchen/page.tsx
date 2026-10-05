// Kitchen tab (lite) - banquet countdown, F&B stats, SLA, orders, channel mix.
//
// Fetches GET /kitchen (via useKitchen) and renders the Kitchen/F&B snapshot.
// The data now lives in the PULSE-owned pulse-kitchen table and is served
// property-scoped over the read-only REST API (Requirement 16.6), replacing the
// curated data previously bundled in the PWA. Loading, empty, and error states
// mirror the Ops tab.

'use client';

import { UtensilsCrossed, Clock, Truck, CalendarClock } from 'lucide-react';
import { useKitchen } from '@/hooks/useKitchen';
import type { KitchenOrder, OrderSlaState } from '@/lib/types';

// SLA state -> text color class for the order elapsed time and label.
const SLA_TEXT: Record<OrderSlaState, string> = {
  'on-time': 'text-success',
  'at-risk': 'text-tier-warning',
  breached: 'text-tier-critical',
};

export default function KitchenPage() {
  const { data, loading, error, refetch } = useKitchen();

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading kitchen...</div>;
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

  const banquet = data?.banquetCountdown ?? null;
  const fbStats = data?.fbStats ?? [];
  const deliverySla = data?.deliverySla ?? null;
  const kitchenOrders = data?.kitchenOrders ?? [];
  const channelMix = data?.channelMix ?? [];
  const channelMixNote = data?.channelMixNote ?? '';

  return (
    <div className="py-4">
      <h2 className="text-lg font-semibold mb-4">Kitchen</h2>

      {/* Banquet countdown */}
      {banquet && (
        <section aria-label="Active banquet" className="mb-5">
          <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400 mb-2">
            Active Banquet
          </h3>
          <div className="rounded-xl border border-accent-secondary/20 bg-gradient-to-br from-accent-secondary/10 to-accent/5 p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="flex items-center gap-1.5 text-sm font-semibold text-ink">
                <CalendarClock size={15} className="text-accent-secondary" aria-hidden />
                {banquet.title}
              </span>
              <span className="text-[9px] font-bold px-2 py-0.5 rounded-full bg-success/15 text-success">
                {banquet.badge}
              </span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-2xl font-black text-ink tabular-nums">
                {banquet.minutesRemaining} min
              </span>
              <div className="flex-1">
                <div className="h-1.5 rounded-full bg-surface-2 overflow-hidden mb-1">
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-accent to-accent-secondary"
                    style={{ width: `${banquet.progressPct}%` }}
                  />
                </div>
                <p className="text-[10px] text-gray-400">{banquet.subline}</p>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* F&B summary */}
      {fbStats.length > 0 && (
        <section aria-label="Food and beverage summary" className="mb-5">
          <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400 mb-2">
            Today&apos;s F&amp;B Summary
          </h3>
          <div className="bg-surface rounded-xl border border-gray-800 p-4 grid grid-cols-3 gap-2">
            {fbStats.map((stat) => (
              <div key={stat.label} className="text-center">
                <p className="text-xl font-bold text-ink tabular-nums">{stat.value}</p>
                <p className="text-[9px] uppercase tracking-wide text-gray-500 mt-0.5">{stat.label}</p>
                <p
                  className={`text-[9px] mt-0.5 ${
                    stat.deltaTone === 'success' ? 'text-success' : 'text-tier-warning'
                  }`}
                >
                  {stat.delta}
                </p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Delivery SLA tracker */}
      {deliverySla && (
        <section aria-label="Delivery SLA" className="mb-5">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400">Delivery SLA</h3>
            <span className="text-[10px] text-accent">{deliverySla.standardLabel}</span>
          </div>
          <div className="bg-surface rounded-xl border border-gray-800 p-4">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs font-semibold text-ink">{deliverySla.label}</span>
              <span className="text-xs font-bold text-tier-warning tabular-nums">{deliverySla.pct}%</span>
            </div>
            <div className="h-1.5 rounded-full bg-surface-2 overflow-hidden mb-3">
              <div
                className="h-full rounded-full bg-gradient-to-r from-success to-tier-warning"
                style={{ width: `${deliverySla.pct}%` }}
              />
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              <div>
                <p className="text-sm font-bold text-ink tabular-nums">{deliverySla.avgLabel}</p>
                <p className="text-[9px] uppercase tracking-wide text-gray-500">Avg Time</p>
              </div>
              <div>
                <p className="text-sm font-bold text-ink tabular-nums">{deliverySla.targetLabel}</p>
                <p className="text-[9px] uppercase tracking-wide text-gray-500">Target</p>
              </div>
              <div>
                <p className="text-sm font-bold text-tier-warning tabular-nums">{deliverySla.atRisk}</p>
                <p className="text-[9px] uppercase tracking-wide text-gray-500">At Risk</p>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* Live order feed */}
      {kitchenOrders.length > 0 && (
        <section aria-label="In-flight orders" className="mb-5">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400">In-Flight Orders</h3>
            <span className="text-[10px] text-gray-500">{kitchenOrders.length} active</span>
          </div>
          <div className="space-y-2">
            {kitchenOrders.map((order) => (
              <OrderRow key={order.id} order={order} />
            ))}
          </div>
        </section>
      )}

      {/* Revenue channel mix */}
      {channelMix.length > 0 && (
        <section aria-label="Revenue channel mix" className="mb-2">
          <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400 mb-2">
            Revenue Channel Mix
          </h3>
          <div className="bg-surface rounded-xl border border-gray-800 p-4">
            <div className="grid grid-cols-4 gap-2 text-center">
              {channelMix.map((slice) => (
                <div key={slice.label}>
                  <p
                    className={`text-base font-bold tabular-nums ${
                      slice.warning ? 'text-tier-warning' : 'text-ink'
                    }`}
                  >
                    {slice.pct}%
                  </p>
                  <p className="text-[9px] uppercase tracking-wide text-gray-500 mt-0.5">{slice.label}</p>
                </div>
              ))}
            </div>
            {channelMixNote && (
              <p className="text-[10px] text-gray-500 mt-3 leading-relaxed">{channelMixNote}</p>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

// A single in-flight order row with a kind icon and SLA-colored elapsed time.
function OrderRow({ order }: { order: KitchenOrder }) {
  const Icon = order.kind === 'external' ? Truck : order.kind === 'banquet' ? CalendarClock : order.kind === 'room-service' ? UtensilsCrossed : Clock;
  return (
    <div className="bg-surface rounded-xl border border-gray-800 p-3 flex items-center gap-3">
      <span aria-hidden className="flex items-center justify-center w-7 h-7 rounded-lg bg-surface-2 text-accent shrink-0">
        <Icon size={15} />
      </span>
      <span className="flex-1 min-w-0">
        <span className="block text-sm font-semibold text-ink truncate">{order.title}</span>
        <span className="block text-[11px] text-gray-500 truncate">{order.detail}</span>
      </span>
      <span className="text-right shrink-0">
        <span className={`block text-sm font-bold tabular-nums ${SLA_TEXT[order.slaState]}`}>
          {order.elapsedLabel}
        </span>
        <span className={`block text-[9px] font-semibold ${SLA_TEXT[order.slaState]}`}>
          {order.slaLabel}
        </span>
      </span>
    </div>
  );
}
