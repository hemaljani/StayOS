// useAlerts - fetch the property-scoped alert feed for the PULSE tab.
//
// Provides the authoritative REST snapshot used by realtime reconciliation and
// manual refresh. Background reads keep existing cards visible; request ordering
// prevents an older response from replacing newer state. The API scopes alerts
// server-side to the caller's properties (Requirement 16.6).

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { authFetch } from '@/lib/api';
import { getCurrentUser, isAuthenticated } from '@/lib/auth';
import type { Alert, AlertsListResponse } from '@/lib/types';

interface AlertsState {
  alerts: Alert[];
  loading: boolean;
  error: string | null;
  snapshotStartedAt: number;
}

export function useAlerts() {
  const [state, setState] = useState<AlertsState>({
    alerts: [],
    loading: true,
    error: null,
    snapshotStartedAt: 0,
  });
  const latestRequest = useRef(0);

  // Fetch (or refetch) the feed. Exposed so callers can refresh after a
  // lifecycle action (approve/acknowledge/resolve) moves a card.
  const refetch = useCallback(async (options: { background?: boolean } = {}) => {
    if (!isAuthenticated()) {
      if (typeof window !== 'undefined') {
        // Not signed in - return to the StayOS shell ("/", outside /pulse basePath).
        window.location.href = '/';
      }
      return;
    }

    const user = getCurrentUser();
    // The feed is property-scoped server-side; pass the caller's property as a
    // hint when known so the query targets it directly.
    const query = user?.propertyId ? `?propertyId=${encodeURIComponent(user.propertyId)}` : '';

    const request = ++latestRequest.current;
    const snapshotStartedAt = Date.now();
    setState((s) => ({ ...s, loading: options.background ? s.loading : true, error: null }));
    try {
      const data = await authFetch<AlertsListResponse>(`/alerts${query}`);
      // A slow older request must not overwrite a newer reconciliation.
      if (request !== latestRequest.current) return;
      setState({ alerts: data.alerts ?? [], loading: false, error: null, snapshotStartedAt });
    } catch (err: unknown) {
      if (request !== latestRequest.current) return;
      const message = err instanceof Error ? err.message : 'Failed to load alerts';
      // A transient REST failure must not erase the currently visible feed.
      setState((s) => ({ ...s, loading: false, error: message }));
    }
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { ...state, refetch };
}
