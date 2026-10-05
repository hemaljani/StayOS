// useShellAuth - client-side auth state for the StayOS shell.
//
// The shell shows the login form when unauthenticated and the feature grid when
// authenticated, so it needs a reactive "am I signed in?" flag plus a login
// action. On successful login it flips to authenticated in place (no redirect) -
// the shell page then renders the grid. Credential errors are surfaced so the
// login form can display them. Sign-out clears the shared session and returns to
// the login view.

'use client';

import { useEffect, useState } from 'react';
import {
  getCurrentUser,
  isAuthenticated,
  signIn,
  signOut,
  type AuthUser,
} from '@/lib/auth';

interface ShellAuthState {
  isLoggedIn: boolean;
  user: AuthUser | null;
  loading: boolean;
  error: string | null;
}

export function useShellAuth() {
  const [state, setState] = useState<ShellAuthState>({
    isLoggedIn: false,
    user: null,
    // Start in loading until the initial client-side session check resolves,
    // preventing a flash of the login form for an already-authenticated GM.
    loading: true,
    error: null,
  });

  // Resolve the existing shared session on mount (SSO: a session established in
  // any StayOS app is visible here).
  useEffect(() => {
    setState({
      isLoggedIn: isAuthenticated(),
      user: getCurrentUser(),
      loading: false,
      error: null,
    });
  }, []);

  const login = async (email: string, password: string): Promise<void> => {
    setState((previous) => ({ ...previous, loading: true, error: null }));
    try {
      await signIn(email, password);
      // Flip to authenticated in place; the shell page renders the grid.
      setState({ isLoggedIn: true, user: getCurrentUser(), loading: false, error: null });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Authentication failed';
      setState((previous) => ({ ...previous, loading: false, error: message }));
    }
  };

  const logout = (): void => {
    signOut();
    setState({ isLoggedIn: false, user: null, loading: false, error: null });
  };

  return { ...state, login, logout };
}
