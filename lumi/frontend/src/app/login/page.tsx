'use client';

import { useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { withBase } from '@/lib/constants';

// LUMI login - FALLBACK prompt only.
//
// The StayOS shell at the site root ("/") is the primary entry point and owns
// the feature launcher grid; LUMI no longer renders that grid. This page exists
// only as a deep-link fallback: if someone lands directly on /lumi/login/
// without a shared StayOS session, they can sign in here. On success useAuth
// navigates to "/" (the shell). A "back to StayOS" link points at the shell.
export default function LoginPage() {
  const { login, loading, error } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await login(email, password);
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-screen -mt-12 -mb-16 px-4">
      {/* Back to the StayOS shell (site root, outside LUMI's routing) - a raw
          anchor, not next/link. */}
      {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- "/" is the
          StayOS shell, not an internal LUMI page. */}
      <a
        href="/"
        className="absolute top-16 left-4 flex items-center gap-1 text-sm text-gray-400 hover:text-ink transition-colors"
      >
        <ArrowLeft size={16} />
        <span>StayOS</span>
      </a>

      {/* Full LUMI logo. Plain <img> (not next/image): with `unoptimized`
          static export, next/image emits the src verbatim and does NOT apply
          basePath, so the /lumi prefix is applied explicitly via withBase(). */}
      {/* eslint-disable-next-line @next/next/no-img-element -- static export;
          basePath-prefixed SVG served from S3/CloudFront, no optimizer. */}
      <img
        src={withBase('/logo.svg')}
        alt="LUMI - Part of StayOS"
        width={200}
        height={72}
        className="mb-2"
      />
      <p className="text-xs text-gray-500 mb-8">The GM Daily Intelligence Brief</p>

      <form onSubmit={handleSubmit} className="w-full max-w-xs space-y-4">
        <label className="sr-only" htmlFor="email">
          Email
        </label>
        <input
          id="email"
          type="email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
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
          onChange={(e) => setPassword(e.target.value)}
          className="w-full bg-surface border border-gray-700 rounded-lg px-4 py-3 text-ink placeholder-gray-500 focus:outline-none focus:border-accent"
          required
          autoComplete="current-password"
        />

        {error && <p role="alert" className="text-danger text-sm text-center">{error}</p>}

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-accent text-white font-semibold py-3 rounded-lg hover:bg-accent/90 transition-colors disabled:opacity-50"
        >
          {loading ? 'Signing in...' : 'Sign In'}
        </button>
      </form>
    </div>
  );
}
