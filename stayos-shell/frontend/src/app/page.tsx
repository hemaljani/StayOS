// StayOS shell root page ("/").
//
// The single StayOS entry point. For an unauthenticated visitor it shows a
// marketing landing page (what StayOS is, plus the two live features LUMI and
// PULSE); a "Sign In" call-to-action swaps that view for the login form in place
// (no redirect). Sign-in establishes the shared StayOS session (localStorage on
// the shared origin), after which LUMI (/lumi) and PULSE (/pulse) read that same
// session with no second login (SSO). An already-authenticated GM lands
// straight on the feature launcher grid.

'use client';

import { useState } from 'react';
import { useShellAuth } from '@/hooks/useShellAuth';
import LandingPage from '@/components/LandingPage';
import LoginForm from '@/components/LoginForm';
import FeatureGrid from '@/components/FeatureGrid';

export default function ShellPage() {
  const { isLoggedIn, user, loading, error, login, logout } = useShellAuth();

  // Which unauthenticated view is showing: the marketing landing page (default)
  // or the login form. Toggled by the landing page's "Sign In" CTA and the
  // login form's "Back to home" control. Ignored once authenticated.
  const [showLogin, setShowLogin] = useState(false);

  // Brief loading state while the initial session check resolves, so an already
  // authenticated GM does not see a flash of the landing page.
  if (loading && !isLoggedIn) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-pulse text-gray-500 text-sm">Loading...</div>
      </div>
    );
  }

  if (isLoggedIn) {
    return <FeatureGrid email={user?.email} name={user?.name} onLogout={logout} />;
  }

  // Unauthenticated: marketing landing page until the visitor chooses to sign in.
  if (!showLogin) {
    return <LandingPage onSignIn={() => setShowLogin(true)} />;
  }

  return (
    <LoginForm onSubmit={login} loading={loading} error={error} onBack={() => setShowLogin(false)} />
  );
}
