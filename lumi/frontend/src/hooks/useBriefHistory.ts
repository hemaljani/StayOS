'use client';

import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { isAuthenticated } from '@/lib/auth';
import type { BriefHistorySummary } from '@/lib/types';

/** Cache TTL: 5 minutes */
const CACHE_TTL_MS = 300_000;

// Module-level cache shared across all consumers of this hook
let cachedData: BriefHistorySummary[] | null = null;
let cachedAt = 0;
let fetchPromise: Promise<BriefHistorySummary[]> | null = null;

interface BriefHistoryState {
  history: BriefHistorySummary[];
  loading: boolean;
  error: string | null;
}

export function useBriefHistory() {
  const [state, setState] = useState<BriefHistoryState>(() => {
    // Initialize from cache if valid (avoids loading flash)
    if (cachedData && Date.now() - cachedAt < CACHE_TTL_MS) {
      return { history: cachedData, loading: false, error: null };
    }
    return { history: [], loading: true, error: null };
  });

  useEffect(() => {
    if (!isAuthenticated()) {
      setState({ history: [], loading: false, error: 'Not authenticated' });
      return;
    }

    // Cache is still valid — nothing to fetch
    if (cachedData && Date.now() - cachedAt < CACHE_TTL_MS) {
      setState({ history: cachedData, loading: false, error: null });
      return;
    }

    const doFetch = async () => {
      try {
        // Deduplication: reuse in-flight request if one exists
        if (!fetchPromise) {
          fetchPromise = authFetch<BriefHistorySummary[]>('/briefs/history?days=7');
        }

        const data = await fetchPromise;
        cachedData = data;
        cachedAt = Date.now();
        setState({ history: data, loading: false, error: null });
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : 'Failed to load history';
        setState({ history: [], loading: false, error: message });
      } finally {
        fetchPromise = null;
      }
    };

    doFetch();
  }, []);

  return state;
}
