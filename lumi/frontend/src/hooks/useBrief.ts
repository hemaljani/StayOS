'use client';

import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { getCurrentUser, isAuthenticated } from '@/lib/auth';
import type { BriefResponse } from '@/lib/types';

interface BriefState {
  brief: BriefResponse | null;
  loading: boolean;
  error: string | null;
}

export function useBrief() {
  const [state, setState] = useState<BriefState>({
    brief: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    // Check authentication before fetching
    if (!isAuthenticated()) {
      // Not signed in - return to the StayOS shell ("/"). Keep loading state to
      // prevent a flash of error.
      if (typeof window !== 'undefined') {
        window.location.href = '/';
      }
      return;
    }

    const user = getCurrentUser();
    if (!user?.propertyId) {
      if (typeof window !== 'undefined') {
        window.location.href = '/';
      }
      return;
    }

    const fetchBrief = async () => {
      try {
        const data = await authFetch<BriefResponse>(`/briefs/${user.propertyId}`);
        setState({ brief: data, loading: false, error: null });
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : 'Failed to load brief';
        setState({ brief: null, loading: false, error: message });
      }
    };

    fetchBrief();
  }, []);

  return state;
}
