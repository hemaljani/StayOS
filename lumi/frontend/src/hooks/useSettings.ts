'use client';

import { useEffect, useState, useCallback } from 'react';
import { authFetch } from '@/lib/api';
import { getCurrentUser } from '@/lib/auth';
import type { GmSettings } from '@/lib/types';

interface SettingsState {
  settings: GmSettings | null;
  loading: boolean;
  error: string | null;
  saving: boolean;
  saveError: string | null;
  saveSuccess: boolean;
}

export function useSettings() {
  const [state, setState] = useState<SettingsState>({
    settings: null,
    loading: true,
    error: null,
    saving: false,
    saveError: null,
    saveSuccess: false,
  });

  useEffect(() => {
    const fetchSettings = async () => {
      const user = getCurrentUser();
      if (!user?.gmAlias) {
        setState((prev) => ({ ...prev, loading: false, error: 'Not authenticated' }));
        return;
      }

      try {
        const data = await authFetch<GmSettings>(`/settings/${user.gmAlias}`);
        setState((prev) => ({ ...prev, settings: data, loading: false, error: null }));
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : 'Failed to load settings';
        setState((prev) => ({ ...prev, loading: false, error: message }));
      }
    };

    fetchSettings();
  }, []);

  const updateSettings = useCallback(async (updates: Partial<GmSettings>) => {
    const user = getCurrentUser();
    if (!user?.gmAlias) return;

    setState((prev) => ({ ...prev, saving: true, saveError: null, saveSuccess: false }));

    try {
      const updated = await authFetch<GmSettings>(`/settings/${user.gmAlias}`, {
        method: 'PUT',
        body: JSON.stringify(updates),
      });
      setState((prev) => ({
        ...prev,
        settings: updated,
        saving: false,
        saveSuccess: true,
        saveError: null,
      }));

      // Clear success state after 3 seconds
      setTimeout(() => {
        setState((prev) => ({ ...prev, saveSuccess: false }));
      }, 3000);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to save settings';
      setState((prev) => ({ ...prev, saving: false, saveError: message, saveSuccess: false }));
    }
  }, []);

  return { ...state, updateSettings };
}
