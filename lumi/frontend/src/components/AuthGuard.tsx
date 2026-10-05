'use client';

import { useEffect, useState } from 'react';
import { usePathname } from 'next/navigation';
import { isAuthenticated, signOut, ACCESS_TOKEN_KEY } from '@/lib/auth';

/**
 * Client-side auth guard that prevents protected content from rendering
 * until authentication is verified. Redirects unauthenticated users to
 * the login page. The login page itself is excluded from the guard.
 *
 * Note: This is UX-only gating — the server validates JWT on every API
 * call regardless. A crafted token could bypass this guard but would get
 * 401s on all data fetches.
 *
 * The session is the shared StayOS session (localStorage, stayos.* namespace),
 * so this guard also watches for cross-app / cross-tab sign-out and periodically
 * re-checks validity to catch an expired session on an idle tab.
 */
// How often to re-check session validity for an open, possibly idle tab (ms).
const SESSION_CHECK_INTERVAL_MS = 30 * 1000;

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [checked, setChecked] = useState(false);
  const [authed, setAuthed] = useState(false);

  // Login page doesn't need auth protection
  const isLoginPage = pathname === '/login' || pathname === '/login/';

  useEffect(() => {
    if (isLoginPage) {
      setChecked(true);
      setAuthed(true);
      return;
    }

    // Revoke access and return to the StayOS shell login (site root "/"). The
    // shell owns login now; "/" is outside LUMI's routing, so this is a raw
    // full-page navigation (not next/router).
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

    // Periodic check: catches an expired session on an idle tab making no API calls.
    const interval = setInterval(() => {
      if (!isAuthenticated()) revoke();
    }, SESSION_CHECK_INTERVAL_MS);

    // Cross-tab / cross-app sign-out: if the shared session token is cleared in
    // another tab or another StayOS app (shell / PULSE), revoke here too. Keyed
    // on the shared namespace (stayos.accessToken) so it fires across the origin.
    const onStorage = (event: StorageEvent) => {
      if (event.key === ACCESS_TOKEN_KEY && !event.newValue) revoke();
    };
    window.addEventListener('storage', onStorage);

    return () => {
      clearInterval(interval);
      window.removeEventListener('storage', onStorage);
    };
  }, [pathname, isLoginPage]);

  // Don't render anything until auth check completes (prevents content flash)
  if (!checked || !authed) {
    if (isLoginPage) return <>{children}</>;
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-pulse text-zinc-500 text-sm">Loading...</div>
      </div>
    );
  }

  return <>{children}</>;
}
