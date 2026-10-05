// AppHeader - fixed top bar for the PULSE PWA.
//
// Mirrors LUMI's header layout (fixed, safe-area aware, max-w-md) but renders the
// PULSE gradient wordmark instead of a logo image. Hidden on the login page. An
// optional property chip shows the active property, and a Logout control cleanly
// ends the session and returns to the StayOS shell.

'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { ArrowLeft, LogOut, Settings } from 'lucide-react';
import { getCurrentUser, signOut } from '@/lib/auth';

interface AppHeaderProps {
  propertyName?: string;
}

/**
 * Derive up-to-two uppercase initials from a display label.
 *
 * Handles both a full name ("Jennifer Smith" -> "JS") and an email
 * ("jsmith@aloha.com" -> "J") so the avatar always shows something sensible.
 */
function initials(label: string): string {
  const source = label.includes('@') ? label.split('@')[0] : label;
  const parts = source.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
  return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
}

export default function AppHeader({ propertyName }: AppHeaderProps) {
  const pathname = usePathname();

  // Account menu: a gear icon opens a dropdown with the property brand, the
  // signed-in identity, and Logout (mirrors the StayOS shell launcher). Closed
  // on outside-click and Escape.
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  // Signed-in identity, read from the shared StayOS session. Resolved on mount
  // (client-only) to avoid touching localStorage during SSR.
  const [identity, setIdentity] = useState<{ name: string; email: string } | null>(null);

  useEffect(() => {
    const user = getCurrentUser();
    if (user) setIdentity({ name: user.name, email: user.email });
  }, []);

  useEffect(() => {
    if (!accountMenuOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setAccountMenuOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [accountMenuOpen]);

  // Hide the header on the login page.
  if (pathname.startsWith('/login')) return null;

  // Cleanly end the session: clear the Cognito tokens, then hard-navigate to the
  // StayOS root ("/"). The StayOS landing (which shows BOTH LUMI and PULSE) lives
  // at the site root, OUTSIDE PULSE's "/pulse" basePath, so this is a raw
  // window.location assignment to "/" (next/router/withBase would stay under
  // /pulse). A full-page load also guarantees all in-memory app state is dropped.
  const handleLogout = () => {
    signOut();
    if (typeof window !== 'undefined') {
      window.location.href = '/';
    }
  };

  return (
    <header className="fixed top-0 left-0 right-0 bg-background/95 backdrop-blur-sm border-b border-gray-800 pt-[var(--sat)] z-50">
      {/* 3-column grid so the PULSE wordmark is truly centered in the bar
          regardless of the left (back link) and right (logout) widths. */}
      <div className="grid grid-cols-3 items-center h-12 px-4 max-w-md mx-auto">
        {/* Left: back to the StayOS shell (feature launcher). The shell lives at
            the site root "/", OUTSIDE PULSE's "/pulse" basePath, so this is a raw
            <a href="/"> (next/link would stay under /pulse). It does NOT sign out
            - the shared session persists, so the shell shows the feature grid and
            the GM can switch to LUMI without re-authenticating. */}
        <div className="justify-self-start">
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- "/" is
              the StayOS shell, outside PULSE's /pulse basePath, not an internal page. */}
          <a
            href="/"
            aria-label="Back to StayOS"
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-ink transition-colors rounded-full px-1.5 py-1 hover:bg-surface"
          >
            <ArrowLeft size={14} aria-hidden />
            <span>StayOS</span>
          </a>
        </div>

        {/* Center: PULSE wordmark - links to the default PULSE view */}
        <Link href="/" className="justify-self-center flex items-center gap-1.5" aria-label="PULSE home">
          <span aria-hidden className="text-lg leading-none">&#9889;</span>
          <span className="text-lg font-black tracking-tight bg-gradient-to-r from-tier-critical via-tier-warning to-tier-info bg-clip-text text-transparent">
            PULSE
          </span>
        </Link>

        {/* Right: account menu (gear icon -> dropdown with property, identity,
            and Logout). Mirrors the StayOS shell launcher's account menu. */}
        <div className="justify-self-end relative z-[60]">
          <button
            type="button"
            onClick={() => setAccountMenuOpen((open) => !open)}
            aria-label="Account menu"
            aria-haspopup="menu"
            aria-expanded={accountMenuOpen}
            className={`relative z-10 flex items-center justify-center rounded-full p-2 text-gray-400 transition-colors hover:bg-surface hover:text-ink ${
              accountMenuOpen ? 'bg-surface text-ink' : ''
            }`}
          >
            <Settings size={18} strokeWidth={1.5} aria-hidden />
          </button>

          {accountMenuOpen && (
            <>
              {/* Transparent full-screen click-catcher: closes on any outside
                  click without dimming the page. Escape also closes (in effect). */}
              <button
                type="button"
                aria-hidden
                tabIndex={-1}
                onClick={() => setAccountMenuOpen(false)}
                className="fixed inset-0 z-0 cursor-default"
              />
              {/* Dropdown card, anchored to the gear's top-right corner. */}
              <div
                role="menu"
                aria-label="Account"
                className="absolute right-0 top-full mt-2 z-10 w-64 origin-top-right rounded-2xl border border-gray-800 bg-surface shadow-2xl shadow-black/40 overflow-hidden"
              >
                {/* Property brand (explicit propertyName when provided). */}
                <div className="px-4 py-3 border-b border-gray-800 bg-background/40">
                  <p className="text-[10px] uppercase tracking-wide text-gray-500 leading-none">
                    Property
                  </p>
                  <p className="mt-1 text-sm font-bold text-ink">
                    {propertyName || 'Aloha Hotels & Resorts'}
                  </p>
                </div>

                {/* Signed-in identity: avatar initials + full name (fallback email). */}
                {(identity?.name || identity?.email) && (
                  <div className="flex items-center gap-3 px-4 py-4 border-b border-gray-800">
                    <span
                      aria-hidden
                      className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-full bg-tier-info/15 text-sm font-bold uppercase text-tier-info"
                    >
                      {initials(identity.name || identity.email)}
                    </span>
                    <div className="min-w-0">
                      <p className="text-[10px] uppercase tracking-wide text-gray-500 leading-none">
                        Signed in as
                      </p>
                      <p className="mt-1 text-base font-semibold text-ink truncate">
                        {identity.name || identity.email}
                      </p>
                      {identity.name && identity.email && (
                        <p className="text-xs text-gray-400 truncate">{identity.email}</p>
                      )}
                    </div>
                  </div>
                )}

                {/* Logout - ends the session and returns to the StayOS shell. */}
                <button
                  type="button"
                  role="menuitem"
                  onClick={handleLogout}
                  aria-label="Log out"
                  className="flex w-full items-center gap-2 px-4 py-3.5 text-sm text-gray-400 transition-colors hover:bg-background hover:text-ink"
                >
                  <LogOut size={15} strokeWidth={1.5} aria-hidden />
                  <span>Logout</span>
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
