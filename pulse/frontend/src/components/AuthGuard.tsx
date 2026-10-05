// AuthGuard - client-side gate that blocks PULSE tabs until auth is verified.
//
// Requirement 16.2/16.3: unauthenticated users are denied all PULSE tabs and
// redirected to /login (the login page itself is exempt and presents the auth
// prompt). Requirement 16.5: when a previously authenticated session expires
// while the app is open, the guard revokes access and returns the user to the
// login prompt. This is UX-only gating - the server validates the JWT on every
// API call regardless (and authFetch handles the 401 refresh/sign-out path).
//
// Session-expiry is caught three ways so a stale tab never keeps access:
//   1. authFetch's 401 -> refresh -> sign-out path (on any API call).
//   2. the proactive token-refresh timer (useTokenRefresh).
//   3. this guard's periodic validity check + cross-tab storage listener, which
//      covers an idle tab making no API calls.

'use client';

import { useEffect, useState } from 'react';
import { usePathname } from 'next/navigation';
import { isAuthenticated, signOut, ACCESS_TOKEN_KEY } from '@/lib/auth';

// How often to re-check session validity for an open, possibly idle tab (ms).
const SESSION_CHECK_INTERVAL_MS = 30 * 1000;

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [checked, setChecked] = useState(false);
  const [authed, setAuthed] = useState(false);

  const isLoginPage = pathname === '/login' || pathname === '/login/';

  useEffect(() => {
    if (isLoginPage) {
      setChecked(true);
      setAuthed(true);
      return;
    }

    // Revoke access and return to the StayOS shell login (site root "/"). The
    // shell owns login now; "/" is outside PULSE's /pulse basePath, so this is a
    // raw redirect (NOT withBase, which would stay under /pulse).
    const revoke = () => {
      signOut();
      window.location.href = '/';
    };

    if (!isAuthenticated()) {
      revoke();
      return;
    }

    setAuthed(true);
    setChecked(true);

    // Periodic check: catches an expired session on an idle tab that is making
    // no API calls (Requirement 16.5).
    const interval = setInterval(() => {
      if (!isAuthenticated()) revoke();
    }, SESSION_CHECK_INTERVAL_MS);

    // Cross-tab / cross-app sign-out: if the shared session token is cleared in
    // another tab or another StayOS app (shell / LUMI), revoke here too. Keyed on
    // the shared namespace (stayos.accessToken) so it fires across the origin.
    const onStorage = (event: StorageEvent) => {
      if (event.key === ACCESS_TOKEN_KEY && !event.newValue) revoke();
    };
    window.addEventListener('storage', onStorage);

    return () => {
      clearInterval(interval);
      window.removeEventListener('storage', onStorage);
    };
  }, [pathname, isLoginPage]);

  // Avoid a flash of protected content before the auth check resolves.
  if (!checked || !authed) {
    if (isLoginPage) return <>{children}</>;
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-pulse text-gray-500 text-sm">Loading...</div>
      </div>
    );
  }

  return <>{children}</>;
}
