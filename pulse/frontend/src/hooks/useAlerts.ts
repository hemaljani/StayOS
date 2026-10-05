// useAlerts - fetch the property-scoped alert feed for the PULSE tab.
//
// This batch (Task 21.1) sources the feed from GET /alerts over REST. Realtime
// updates (AppSync Events WebSocket) that push create/status-change/resolve
// events into this state are wired in a later batch (Task 21.3); until then the
// feed reflects the point-in-time REST read plus a manual refetch. The API
// scopes alerts server-side to the caller's properties (Requirement 16.6), so
// the client simply consumes what it is given.

'use client';

import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { getCurrentUser, isAuthenticated } from '@/lib/auth';
import type { Alert, AlertsListResponse } from '@/lib/types';

interface AlertsState {
  alerts: Alert[];
  loading: boolean;
  error: string | null;
}

export function useAlerts() {
  const [state, setState] = useState<AlertsState>({
    alerts: [],
    loading: true,
    error: null,
  });

  // Fetch (or refetch) the feed. Exposed so callers can refresh after a
  // lifecycle action (approve/acknowledge/resolve) moves a card.
  const refetch = useCallback(async () => {
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

    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await authFetch<AlertsListResponse>(`/alerts${query}`);
      setState({ alerts: data.alerts ?? [], loading: false, error: null });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load alerts';
      setState({ alerts: [], loading: false, error: message });
    }
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { ...state, refetch };
}
