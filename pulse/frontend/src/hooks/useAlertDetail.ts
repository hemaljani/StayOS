// useAlertDetail - fetch a single alert (including its triage brief) on demand.
//
// The triage modal calls GET /alerts/{alertId} when a card is opened to load the
// full brief (Requirement 15.7). Although the feed items already carry the
// triageBrief, Task 21.1 specifies fetching the full detail so the modal always
// reflects the latest server state (e.g. an approval recorded from another
// device). The hook is lazy: it only fetches when given a non-null alertId.

'use client';

import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import type { Alert, AlertDetailResponse } from '@/lib/types';

interface AlertDetailState {
  alert: Alert | null;
  loading: boolean;
  error: string | null;
}

export function useAlertDetail(alertId: string | null) {
  const [state, setState] = useState<AlertDetailState>({
    alert: null,
    loading: false,
    error: null,
  });

  useEffect(() => {
    if (!alertId) {
      setState({ alert: null, loading: false, error: null });
      return;
    }

    let cancelled = false;
    setState({ alert: null, loading: true, error: null });

    authFetch<AlertDetailResponse>(`/alerts/${encodeURIComponent(alertId)}`)
      .then((data) => {
        if (!cancelled) {
          setState({ alert: data.alert, loading: false, error: null });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : 'Failed to load alert detail';
          setState({ alert: null, loading: false, error: message });
        }
      });

    // Avoid setting state after the modal closes / alert changes.
    return () => {
      cancelled = true;
    };
  }, [alertId]);

  return state;
}
