// LoginForm - the StayOS shell's unauthenticated view.
//
// Authenticates against the shared StayOS (LUMI) Cognito user pool. On invalid
// credentials the GM is retained on this prompt and a visible error is shown; on
// success the parent shell flips to the feature grid (no redirect). Mirrors the
// StayOS branding used by the LUMI/PULSE login pages.

'use client';

import { useState } from 'react';
import StayOSLogo from '@/components/StayOSLogo';

interface LoginFormProps {
  // Delegated to the shell auth hook; resolves once Cognito responds.
  onSubmit: (email: string, password: string) => void | Promise<void>;
  loading: boolean;
  error: string | null;
  // Optional: returns to the marketing landing page. When provided, a "Back"
  // affordance is rendered so a visitor can leave the login form.
  onBack?: () => void;
}

export default function LoginForm({ onSubmit, loading, error, onBack }: LoginFormProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    await onSubmit(email, password);
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-screen px-4">
      {/* StayOS logo lockup */}
      <StayOSLogo size={64} wordmarkClassName="text-3xl" />
      <p className="text-sm text-ink-soft mb-1 mt-3">The Operating System for Hotel Associates</p>
      <p className="text-xs text-ink-faint mb-8">Sign in once to access every feature</p>

      <form onSubmit={handleSubmit} className="w-full max-w-xs space-y-4">
        <label className="sr-only" htmlFor="email">
          Email
        </label>
        <input
          id="email"
          type="email"
          placeholder="Email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="w-full bg-surface border border-line rounded-lg px-4 py-3 text-ink placeholder-ink-faint focus:outline-none focus:border-accent shadow-sm"
          required
          autoComplete="email"
        />

        <label className="sr-only" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          placeholder="Password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="w-full bg-surface border border-line rounded-lg px-4 py-3 text-ink placeholder-ink-faint focus:outline-none focus:border-accent shadow-sm"
          required
          autoComplete="current-password"
        />

        {/* Visible credentials error indication. */}
        {error && (
          <p role="alert" className="text-danger text-sm text-center">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-accent text-white font-semibold py-3 rounded-lg shadow-lg shadow-accent/25 hover:bg-accent-deep transition-colors disabled:opacity-50"
        >
          {loading ? 'Signing in...' : 'Sign In'}
        </button>
      </form>

      {/* Return to the marketing landing page (only when a handler is wired). */}
      {onBack && (
        <button
          type="button"
          onClick={onBack}
          className="text-xs text-ink-soft hover:text-ink transition-colors mt-6"
        >
          &larr; Back to home
        </button>
      )}

      <p className="text-[10px] text-ink-faint mt-8">&copy; 2026 Aloha Hotels &amp; Resorts</p>
      <p className="text-[9px] text-ink-faint/70 mt-1 text-center max-w-xs">
        Aloha Hotels &amp; Resorts is a fictional brand for demo purposes only.
      </p>
    </div>
  );
}
