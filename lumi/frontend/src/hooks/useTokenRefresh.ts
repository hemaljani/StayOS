'use client';

import { useEffect } from 'react';
import { refreshSession, isAuthenticated, signOut } from '@/lib/auth';

// Refresh token 10 minutes before expiry (tokens last 60 min, refresh at 50 min)
const REFRESH_INTERVAL_MS = 50 * 60 * 1000;

/**
 * Proactively refreshes the Cognito access token before it expires.
 * Runs a timer that calls refreshSession() every 50 minutes.
 * If refresh fails, redirects to login.
 */
export function useTokenRefresh(): void {
  useEffect(() => {
    if (!isAuthenticated()) return;

    const interval = setInterval(async () => {
      const result = await refreshSession();
      if (!result) {
        // Refresh failed — session expired, return to the StayOS shell ("/")
        signOut();
        window.location.href = '/';
      }
    }, REFRESH_INTERVAL_MS);

    return () => clearInterval(interval);
  }, []);
}
