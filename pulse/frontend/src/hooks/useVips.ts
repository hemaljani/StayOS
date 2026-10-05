// useVips - fetch the property-scoped VIP arrivals for the VIPs tab (Task 21.2).
//
// Mirrors useAlerts: reads GET /vips over REST via authFetch, scoped server-side
// to the caller's property (Requirement 16.6). The facade groups arrivals by
// loyalty tier ordered by eliteness and strips sensitiveNotes before it reaches
// the client (Requirement 15.9, 15.10). Exposes a refetch so the tab can retry
// after an error.

'use client';

import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { getCurrentUser, isAuthenticated } from '@/lib/auth';
import type { VipsResponse } from '@/lib/types';

interface VipsState {
  data: VipsResponse | null;
  loading: boolean;
  error: string | null;
}

export function useVips() {
  const [state, setState] = useState<VipsState>({
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
    // The route is property-scoped server-side; pass the caller's property as a
    // hint when known so the query targets it directly.
    const query = user?.propertyId ? `?propertyId=${encodeURIComponent(user.propertyId)}` : '';

    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await authFetch<VipsResponse>(`/vips${query}`);
      setState({ data, loading: false, error: null });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load VIP arrivals';
      setState({ data: null, loading: false, error: message });
    }
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { ...state, refetch };
}
