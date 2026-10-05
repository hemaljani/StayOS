// useKitchen - fetch the property-scoped Kitchen/F&B snapshot for the Kitchen tab.
//
// Mirrors useOps/useVips: reads GET /kitchen over REST via authFetch, scoped
// server-side to the caller's property (Requirement 16.6). The snapshot (banquet
// countdown, F&B stats, delivery SLA, in-flight orders, channel mix) is now
// served from the PULSE-owned pulse-kitchen table instead of being bundled in
// the PWA. Exposes a refetch for error retries.

'use client';

import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { getCurrentUser, isAuthenticated } from '@/lib/auth';
import type { KitchenResponse } from '@/lib/types';

interface KitchenState {
  data: KitchenResponse | null;
  loading: boolean;
  error: string | null;
}

export function useKitchen() {
  const [state, setState] = useState<KitchenState>({
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
    // The route requires propertyId and is property-scoped server-side; pass the
    // caller's property so the query targets it directly.
    const query = user?.propertyId ? `?propertyId=${encodeURIComponent(user.propertyId)}` : '';

    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await authFetch<KitchenResponse>(`/kitchen${query}`);
      setState({ data, loading: false, error: null });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load kitchen data';
      setState({ data: null, loading: false, error: message });
    }
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { ...state, refetch };
}
