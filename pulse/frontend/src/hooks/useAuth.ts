// useAuth - client-side authentication state for the PULSE PWA.
//
// Mirrors LUMI's hook: exposes login/logout and the current user derived from the
// Cognito idToken. On successful login it navigates to the PULSE default view
// ("/"). Credential errors are surfaced so the login form can display them
// (Requirement 16.4).

'use client';

import { useEffect, useState } from 'react';
import { getCurrentUser, isAuthenticated, signIn, signOut, type AuthUser } from '@/lib/auth';
import { withBase } from '@/lib/constants';

interface AuthState {
  isLoggedIn: boolean;
  user: AuthUser | null;
  loading: boolean;
  error: string | null;
}

export function useAuth() {
  const [state, setState] = useState<AuthState>({
    isLoggedIn: false,
    user: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    const user = getCurrentUser();
    setState({
      isLoggedIn: isAuthenticated(),
      user,
      loading: false,
      error: null,
    });
  }, []);

  const login = async (email: string, password: string) => {
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      await signIn(email, password);
      const user = getCurrentUser();
      setState({ isLoggedIn: true, user, loading: false, error: null });
      // This hook backs the PULSE deep-link login fallback (/pulse/login/); on
      // success land in PULSE itself. Full-page navigation is reliable under
      // static export.
      window.location.href = withBase('/');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Authentication failed';
      setState((s) => ({ ...s, loading: false, error: message }));
    }
  };

  const logout = () => {
    signOut();
    setState({ isLoggedIn: false, user: null, loading: false, error: null });
    // Return to the StayOS shell ("/"), which owns login. "/" is outside PULSE's
    // /pulse basePath, so a raw full-page navigation (not next/router/withBase).
    if (typeof window !== 'undefined') {
      window.location.href = '/';
    }
  };

  return { ...state, login, logout };
}
