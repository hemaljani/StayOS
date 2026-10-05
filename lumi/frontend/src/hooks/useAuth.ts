'use client';

import { useEffect, useState } from 'react';
import { getCurrentUser, isAuthenticated, signIn, signOut } from '@/lib/auth';

interface AuthState {
  isLoggedIn: boolean;
  user: { email: string; name: string; gmAlias: string; propertyId: string } | null;
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
      // Use window.location for reliable navigation in static export
      window.location.href = '/';
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Authentication failed';
      setState((s) => ({ ...s, loading: false, error: message }));
    }
  };

  const logout = () => {
    signOut();
    setState({ isLoggedIn: false, user: null, loading: false, error: null });
    // Return to the StayOS shell ("/"), which owns login. A full-page navigation
    // (not next/router) because "/" is outside LUMI's routing and guarantees all
    // in-memory state is dropped.
    if (typeof window !== 'undefined') {
      window.location.href = '/';
    }
  };

  return { ...state, login, logout };
}
