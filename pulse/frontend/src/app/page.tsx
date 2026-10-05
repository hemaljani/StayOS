// PULSE tab - the default PULSE view (Requirement 15.2, 15.4).
//
// Renders the KPI grid, the tier filter (All/Critical/Warning/Info), the live
// alert feed, and the resolved history section. RESOLVED alerts are excluded
// from the live feed and shown only in resolved history (Requirement 12.4,
// Property 20). Selecting a tier shows only alerts of that tier; All shows every
// tier (Requirement 15.5, Property 23). When no alerts match the selected tier
// an empty-state message is shown while the filter selection is retained
// (Requirement 15.6). Selecting a card opens the triage modal (Requirement 15.7).
//
// The feed is loaded via GET /alerts (REST) and kept live by AppSync Events
// (Task 21.3): ALERT_CREATED/UPDATED/RESOLVED events update the feed and KPIs in
// place without polling, honoring the active tier filter, with graceful fallback
// to the manual refresh when realtime config is missing.

'use client';

import { useEffect, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useRealtimeAlerts } from '@/hooks/useRealtimeAlerts';
import KpiGrid from '@/components/KpiGrid';
import TierFilter, { type TierFilterValue } from '@/components/TierFilter';
import AlertCard from '@/components/AlertCard';
import TriageModal from '@/components/TriageModal';
import GenerateAlertsButton from '@/components/GenerateAlertsButton';
import ThemeToggle from '@/components/ThemeToggle';
import { partitionAlertsByStatus, filterAlertsByTier } from '@/lib/format';
import { withBase } from '@/lib/constants';
import type { Alert } from '@/lib/types';

export default function PulsePage() {
  const { alerts, loading, error, refetch, assignedIds, connected } = useRealtimeAlerts();
  const [tierFilter, setTierFilter] = useState<TierFilterValue>('ALL');
  const [selectedAlert, setSelectedAlert] = useState<Alert | null>(null);

  // Deep-link support (Task 21.3): a notificationclick opens /?alertId=..., so
  // open the matching alert's triage modal once the feed has loaded. The
  // parameter is consumed once to avoid re-opening after the GM closes it.
  useEffect(() => {
    if (typeof window === 'undefined' || alerts.length === 0) return;
    const params = new URLSearchParams(window.location.search);
    const alertId = params.get('alertId');
    if (!alertId) return;
    const match = alerts.find((alert) => alert.alertId === alertId);
    if (match) setSelectedAlert(match);
    // Clear the query so it does not reopen on the next render/navigation.
    params.delete('alertId');
    const query = params.toString();
    window.history.replaceState(null, '', withBase(query ? `/?${query}` : '/'));
    // Intentionally runs when alerts first populate.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alerts.length]);

  // Auto-refresh (every 30s): periodically re-pull the feed from the REST API so
  // the data stays current even if the realtime WebSocket misses an event (e.g.
  // a failed push). This complements — does not replace — the manual Refresh
  // button, which stays available. refetch() reconciles into the live-merged
  // list (briefs preserved), so a poll never regresses an agent-ready card.
  // Skipped while the tab is hidden to avoid needless calls in the background.
  const AUTO_REFRESH_MS = 30_000;
  useEffect(() => {
    const tick = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      refetch();
    };
    const interval = setInterval(tick, AUTO_REFRESH_MS);
    // Refresh immediately when the tab regains focus so a returning GM is current.
    const onVisible = () => {
      if (document.visibilityState === 'visible') refetch();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refetch]);

  // Partition the feed into live vs resolved by status (Property 20). RESOLVED
  // alerts are excluded from the live feed and shown only in resolved history.
  const { live: liveBase, resolved: resolvedBase } = useMemo(
    () => partitionAlertsByStatus(alerts),
    [alerts]
  );

  // Live feed = non-resolved alerts. Escalation-assigned alerts are pinned to the
  // top; the remainder are ordered newest first.
  const liveAlerts = useMemo(
    () =>
      [...liveBase].sort((a, b) => {
        const aAssigned = assignedIds.has(a.alertId) ? 1 : 0;
        const bAssigned = assignedIds.has(b.alertId) ? 1 : 0;
        if (aAssigned !== bAssigned) return bAssigned - aAssigned;
        return new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime();
      }),
    [liveBase, assignedIds]
  );

  // Resolved history = resolved alerts, most recently resolved first.
  const resolvedAlerts = useMemo(
    () =>
      [...resolvedBase].sort(
        (a, b) =>
          new Date(b.resolvedAt ?? b.createdAt).getTime() -
          new Date(a.resolvedAt ?? a.createdAt).getTime()
      ),
    [resolvedBase]
  );

  // Counts per filter value, computed on live alerts (used by the filter badges).
  const counts = useMemo<Record<TierFilterValue, number>>(
    () => ({
      ALL: liveAlerts.length,
      CRITICAL: liveAlerts.filter((a) => a.tier === 'CRITICAL').length,
      WARNING: liveAlerts.filter((a) => a.tier === 'WARNING').length,
      INFO: liveAlerts.filter((a) => a.tier === 'INFO').length,
    }),
    [liveAlerts]
  );

  // Apply the tier filter to the live feed (Property 23).
  const filteredLive = useMemo(
    () => filterAlertsByTier(liveAlerts, tierFilter),
    [liveAlerts, tierFilter]
  );

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading alerts...</div>;
  }

  if (error) {
    return (
      <div className="py-8 text-center">
        <p className="text-danger mb-3">{error}</p>
        <button
          type="button"
          onClick={() => refetch()}
          className="text-sm text-accent underline"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div className="py-4">
      {/* Top row: theme toggle at the top-right of the home page. */}
      <div className="mb-3 flex items-center justify-end">
        <ThemeToggle />
      </div>

      {/* KPI grid */}
      <KpiGrid alerts={alerts} />

      {/* Tier filter */}
      <div className="mb-2">
        <TierFilter value={tierFilter} onChange={setTierFilter} counts={counts} />
      </div>

      {/* Live status row */}
      <div className="flex items-center justify-between mb-3">
        <span className="flex items-center gap-1.5 text-[10px] font-semibold text-success">
          <span
            className={`w-1.5 h-1.5 rounded-full bg-success ${connected ? 'animate-pulse' : 'opacity-40'}`}
            aria-hidden
          />
          {connected ? 'Monitoring live' : 'Monitoring'}
        </span>
        <div className="flex items-center gap-3">
          <GenerateAlertsButton onGenerated={refetch} />
          <button
            type="button"
            onClick={() => refetch()}
            aria-label="Refresh alerts"
            className="flex items-center gap-1 text-[10px] text-gray-500 hover:text-ink"
          >
            <RefreshCw size={12} aria-hidden /> Refresh
          </button>
        </div>
      </div>

      {/* Live feed */}
      {filteredLive.length > 0 ? (
        <div className="space-y-2">
          {filteredLive.map((alert) => (
            <AlertCard
              key={alert.alertId}
              alert={alert}
              onSelect={setSelectedAlert}
              assigned={assignedIds.has(alert.alertId)}
            />
          ))}
        </div>
      ) : (
        // Empty-state (Requirement 15.6): message shown while the filter is retained.
        <div className="bg-surface rounded-xl p-6 text-center border border-gray-800">
          <p className="text-sm text-gray-400">
            {tierFilter === 'ALL'
              ? 'No active alerts. All clear.'
              : `No matching ${tierFilter.toLowerCase()} alerts.`}
          </p>
        </div>
      )}

      {/* Resolved history */}
      {resolvedAlerts.length > 0 && (
        <div className="mt-6">
          <div className="flex items-center gap-3 mb-3">
            <span className="flex-1 h-px bg-gray-800" />
            <span className="text-[10px] font-bold uppercase tracking-wide text-gray-500">
              Resolved
            </span>
            <span className="flex-1 h-px bg-gray-800" />
          </div>
          <div className="space-y-2">
            {resolvedAlerts.map((alert) => (
              <AlertCard key={alert.alertId} alert={alert} onSelect={setSelectedAlert} />
            ))}
          </div>
        </div>
      )}

      {/* Triage modal */}
      {selectedAlert && (
        <TriageModal
          alert={selectedAlert}
          onClose={() => setSelectedAlert(null)}
          onActionComplete={refetch}
        />
      )}
    </div>
  );
}
