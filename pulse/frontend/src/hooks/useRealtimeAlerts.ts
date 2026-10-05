// useRealtimeAlerts - the PULSE feed with live AppSync Events updates (Task 21.3).
//
// Layers realtime on top of the REST baseline (useAlerts): it seeds from
// GET /alerts, then applies ALERT_CREATED / ALERT_UPDATED / ALERT_RESOLVED events
// pushed over the AppSync Events WebSocket so the live feed and KPI grid update
// without polling (Requirement 15.4, design Component 6). ALERT_CREATED prepends
// (or updates) a card; ALERT_UPDATED updates escalation/brief/status in place and
// flips the agent-ready badge when hasTriageBrief becomes true; ALERT_RESOLVED
// moves the card to resolved history. KPI counts are derived from the alert list
// downstream, so they update automatically.
//
// Escalation events arriving on the caller's per-user channel are recorded in
// assignedIds so the feed can give them a distinct "assigned to you" treatment,
// separate from ambient feed updates. On each (re)subscribe the hook reconciles
// via GET /alerts so no state is missed during a connection gap. When realtime
// config is missing the hook silently falls back to the REST baseline + manual
// refresh.

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAlerts } from './useAlerts';
import { getCurrentUser } from '@/lib/auth';
import { RealtimeClient, fetchRealtimeConfig } from '@/lib/realtime';
import type { Alert, RealtimeAlertEvent } from '@/lib/types';

// A minimal placeholder brief so the agent-ready badge can appear when a realtime
// event reports hasTriageBrief=true before the full brief is fetched on open.
// The triage modal replaces this by fetching GET /alerts/{alertId}.
function placeholderBrief(): Alert['triageBrief'] {
  return { summary: 'Agent triage ready', confidence: 0, options: [] };
}

// Synthesize a feed-ready Alert from an ALERT_CREATED event (the event is
// "full-enough" for a card; the full detail is fetched on open). An optional
// `existing` alert (present when we are re-folding an event for a card that was
// already in the list) lets us PRESERVE a triage brief the card already carries,
// so a brief-free event never downgrades a card that the REST snapshot (or an
// earlier ALERT_UPDATED) already marked agent-ready.
function alertFromEvent(event: RealtimeAlertEvent, existing?: Alert): Alert {
  const timestamp = event.lastStatusChangeAt ?? new Date().toISOString();
  return {
    alertId: event.alertId,
    propertyId: event.propertyId,
    tier: event.tier ?? existing?.tier ?? 'INFO',
    type: event.type ?? existing?.type ?? 'VIP_CHECKIN',
    title: event.title ?? existing?.title ?? 'New alert',
    detail: existing?.detail ?? '',
    status: event.status ?? existing?.status ?? 'UNACKNOWLEDGED',
    createdAt: existing?.createdAt ?? timestamp,
    dedupeKey: existing?.dedupeKey ?? '',
    escalationStatus: event.escalationStatus ?? existing?.escalationStatus,
    // Keep an existing brief; only add the placeholder when the event reports a
    // brief and the card does not already have one. Never null a real brief.
    triageBrief:
      existing?.triageBrief ?? (event.hasTriageBrief ? placeholderBrief() : null),
    lastStatusChangeAt: event.lastStatusChangeAt ?? existing?.lastStatusChangeAt ?? null,
  };
}

// Apply an event onto the current alert list, returning a new list. Exported so
// the realtime merge behavior (add on ALERT_CREATED, move to resolved on
// ALERT_RESOLVED) can be unit/property tested directly without a live socket.
export function mergeEvent(prev: Alert[], event: RealtimeAlertEvent): Alert[] {
  const index = prev.findIndex((alert) => alert.alertId === event.alertId);

  if (event.eventType === 'ALERT_CREATED' && index === -1) {
    // New alert: prepend so it sorts to the top of the live feed.
    return [alertFromEvent(event), ...prev];
  }

  if (index === -1) {
    // UPDATED/RESOLVED for an alert not in the current window: fold it in so the
    // event is not lost (it reconciles fully on the next GET /alerts).
    return [alertFromEvent(event), ...prev];
  }

  // Update the existing card in place.
  const existing = prev[index];
  const updated: Alert = {
    ...existing,
    tier: event.tier ?? existing.tier,
    type: event.type ?? existing.type,
    title: event.title ?? existing.title,
    status: event.eventType === 'ALERT_RESOLVED' ? 'RESOLVED' : event.status ?? existing.status,
    escalationStatus: event.escalationStatus ?? existing.escalationStatus,
    lastStatusChangeAt: event.lastStatusChangeAt ?? existing.lastStatusChangeAt,
    // Preserve any brief the card already has; add the placeholder when the
    // event reports a brief is now available. A brief-free event (e.g. an early
    // status change) never clears an existing brief.
    triageBrief:
      existing.triageBrief ?? (event.hasTriageBrief ? placeholderBrief() : null),
  };
  if (event.eventType === 'ALERT_RESOLVED') {
    updated.resolvedAt = event.lastStatusChangeAt ?? new Date().toISOString();
  }

  const next = [...prev];
  next[index] = updated;
  return next;
}

export function useRealtimeAlerts() {
  const base = useAlerts();
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [assignedIds, setAssignedIds] = useState<Set<string>>(new Set());
  const [connected, setConnected] = useState(false);

  // Keep the latest refetch in a ref so the realtime effect can call it without
  // re-subscribing when the callback identity changes.
  const refetchRef = useRef(base.refetch);
  refetchRef.current = base.refetch;

  // Seed / reconcile the local list from every REST snapshot (baseline + the
  // reconciliation refetch fired on each resubscribe). The REST snapshot is
  // authoritative and carries the full triage brief, so we take it as the base;
  // but if a card was marked agent-ready by a realtime ALERT_UPDATED while a
  // (possibly earlier) REST fetch returned it without a brief, we keep that
  // brief so the badge never regresses across a reseed.
  useEffect(() => {
    setAlerts((prev) => {
      if (prev.length === 0) return base.alerts;
      const priorById = new Map(prev.map((alert) => [alert.alertId, alert]));
      return base.alerts.map((alert) => {
        if (alert.triageBrief) return alert;
        const prior = priorById.get(alert.alertId);
        return prior?.triageBrief ? { ...alert, triageBrief: prior.triageBrief } : alert;
      });
    });
  }, [base.alerts]);

  const applyEvent = useCallback((event: RealtimeAlertEvent, assigned: boolean) => {
    setAlerts((prev) => mergeEvent(prev, event));
    if (assigned) {
      setAssignedIds((prev) => {
        const next = new Set(prev);
        next.add(event.alertId);
        return next;
      });
    }
  }, []);

  // Open the realtime subscription once on mount.
  useEffect(() => {
    let client: RealtimeClient | null = null;
    let cancelled = false;

    (async () => {
      const config = await fetchRealtimeConfig();
      if (cancelled || !config) return; // Graceful fallback: manual refresh only.
      const user = getCurrentUser();
      if (!user?.propertyId) return;

      client = new RealtimeClient(config, user.propertyId, user.gmAlias, {
        onBroadcastEvent: (event) => applyEvent(event, false),
        onUnicastEvent: (event) => applyEvent(event, true),
        onResubscribe: () => refetchRef.current(),
        onConnectionChange: setConnected,
      });
      client.start();
    })();

    return () => {
      cancelled = true;
      client?.stop();
    };
  }, [applyEvent]);

  return {
    alerts,
    loading: base.loading,
    error: base.error,
    refetch: base.refetch,
    assignedIds,
    connected,
  };
}
