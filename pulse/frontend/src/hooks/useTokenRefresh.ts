// useTokenRefresh - proactively refresh the Cognito session before expiry.
//
// Mirrors LUMI: runs a timer that refreshes the access/id token every 50 minutes
// (tokens last ~60 min). If a refresh fails, the session is cleared and the user
// is redirected to login (Requirement 16.5), complementing the AuthGuard's
// periodic session check and authFetch's 401 sign-out path.

'use client';

import { useEffect } from 'react';
import { isAuthenticated, refreshSession, signOut } from '@/lib/auth';

// Refresh 10 minutes before the ~60-minute token lifetime elapses.
const REFRESH_INTERVAL_MS = 50 * 60 * 1000;

export function useTokenRefresh(): void {
  useEffect(() => {
    if (!isAuthenticated()) return;

    const interval = setInterval(async () => {
      const result = await refreshSession();
      if (!result) {
        // Refresh failed - return to the StayOS shell ("/"), which owns login.
        // "/" is outside PULSE's /pulse basePath, so a raw redirect (not withBase).
        signOut();
        window.location.href = '/';
      }
    }, REFRESH_INTERVAL_MS);

    return () => clearInterval(interval);
  }, []);
}
