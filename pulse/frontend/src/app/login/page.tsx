// Login page for the PULSE PWA (Requirement 16.1, 16.3, 16.4).
//
// Authenticates against the existing LUMI Cognito user pool with StayOS
// credentials. On invalid credentials the user is retained on this prompt and an
// error indication is shown (Requirement 16.4). Mirrors LUMI's login form
// styling with the PULSE gradient wordmark.

'use client';

import { useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';

export default function LoginPage() {
  const { login, loading, error } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    await login(email, password);
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-screen -mt-12 -mb-16 px-4">
      {/* Back to the StayOS shell. PULSE is served under /pulse; the StayOS
          landing (LUMI feature selection) lives at the site root, which is
          outside PULSE's basePath, so this is a raw anchor to "/" rather than
          next/link (which would prefix it with /pulse). Mirrors LUMI's login
          "back to StayOS" affordance. */}
      {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- "/" is the
          StayOS shell outside PULSE's /pulse basePath, not an internal page. */}
      <a
        href="/"
        className="absolute top-16 left-4 flex items-center gap-1 text-sm text-gray-400 hover:text-ink transition-colors"
      >
        <ArrowLeft size={16} />
        <span>StayOS</span>
      </a>

      {/* PULSE wordmark */}
      <div className="flex items-center gap-2 mb-1">
        <span aria-hidden className="text-2xl leading-none">&#9889;</span>
        <span className="text-3xl font-black tracking-tight bg-gradient-to-r from-tier-critical via-tier-warning to-tier-info bg-clip-text text-transparent">
          PULSE
        </span>
      </div>
      <p className="text-[11px] uppercase tracking-widest text-warning font-bold mb-1">StayOS</p>
      <p className="text-xs text-gray-500 mb-8">Real-Time Situational Awareness</p>

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
          className="w-full bg-surface border border-gray-700 rounded-lg px-4 py-3 text-ink placeholder-gray-500 focus:outline-none focus:border-accent"
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
          className="w-full bg-surface border border-gray-700 rounded-lg px-4 py-3 text-ink placeholder-gray-500 focus:outline-none focus:border-accent"
          required
          autoComplete="current-password"
        />

        {/* Credentials error indication (Requirement 16.4) */}
        {error && (
          <p role="alert" className="text-danger text-sm text-center">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-accent text-white font-semibold py-3 rounded-lg hover:bg-accent/90 transition-colors disabled:opacity-50"
        >
          {loading ? 'Signing in...' : 'Sign In'}
        </button>
      </form>

      <p className="text-[10px] text-gray-600 mt-8">&copy; 2026 Aloha Hotels &amp; Resorts</p>
      <p className="text-[9px] text-gray-700 mt-1 text-center max-w-xs">
        Aloha Hotels &amp; Resorts is a fictional brand for demo purposes only.
      </p>
    </div>
  );
}
