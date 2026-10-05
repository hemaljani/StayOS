// useOps - fetch the property-scoped operations snapshot for the Ops tab (Task 21.2).
//
// Mirrors useAlerts/useVips: reads GET /ops over REST via authFetch, scoped
// server-side to the caller's property (Requirement 16.6). The facade composes
// the facility summary, OOO room cards (each joined with its work-order status),
// and the group-checkout summary (Requirement 15.11). Exposes a refetch for
// error retries.

'use client';

import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { getCurrentUser, isAuthenticated } from '@/lib/auth';
import type { OpsResponse } from '@/lib/types';

interface OpsState {
  data: OpsResponse | null;
  loading: boolean;
  error: string | null;
}

export function useOps() {
  const [state, setState] = useState<OpsState>({
    data: null,
    loading: true,
    error: null,
  });

  const refetch = useCallback(async () => {
    if (!isAuthenticated()) {
      if (typeof window !== 'undefined') {
        // Not signed in - return to the StayOS shell ("/", outside /pulse basePath).
        window.location.href = '/';
      }
      return;
    }

    const user = getCurrentUser();
    // Property-scoped server-side; pass the caller's property as a hint.
    const query = user?.propertyId ? `?propertyId=${encodeURIComponent(user.propertyId)}` : '';

    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await authFetch<OpsResponse>(`/ops${query}`);
      setState({ data, loading: false, error: null });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load operations';
      setState({ data: null, loading: false, error: message });
    }
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { ...state, refetch };
}
