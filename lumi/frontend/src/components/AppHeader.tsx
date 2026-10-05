'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { ArrowLeft, Settings } from 'lucide-react';

interface AppHeaderProps {
  propertyName?: string;
}

export default function AppHeader({ propertyName }: AppHeaderProps) {
  const pathname = usePathname();

  // Hide header on login page
  if (pathname.startsWith('/login')) return null;

  return (
    <header className="fixed top-0 left-0 right-0 bg-background/95 backdrop-blur-sm border-b border-gray-800 pt-[var(--sat)] z-50">
      {/* 3-column grid so the LUMI logo is truly centered in the bar regardless
          of the left (back link) and right (settings) widths. */}
      <div className="grid grid-cols-3 items-center h-12 px-4 max-w-md mx-auto">
        {/* Left: back to the StayOS shell (feature launcher). The shell lives at
            the site root "/", OUTSIDE LUMI's "/lumi" basePath, so this is a raw
            <a href="/"> (next/link would stay under /lumi). It does NOT sign out
            - the shared session persists, so the shell shows the feature grid and
            the GM can switch to PULSE without re-authenticating. */}
        <div className="justify-self-start">
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- "/" is
              the StayOS shell, outside LUMI's /lumi basePath, not an internal page. */}
          <a
            href="/"
            aria-label="Back to StayOS"
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-ink transition-colors rounded-full px-1.5 py-1 hover:bg-surface"
          >
            <ArrowLeft size={14} aria-hidden />
            <span>StayOS</span>
          </a>
        </div>

        {/* Center: LUMI compact logo, links to LUMI home. Inlined SVG (not an
            <img>) so the "LUMI" wordmark inherits the theme ink color via
            currentColor — it stays readable in BOTH light and dark, unlike the
            old white-filled asset which vanished on the light canvas. The sun
            mark keeps its gradient; the StayOS subtext uses the accent. */}
        <Link href="/" className="justify-self-center text-ink" aria-label="LUMI home">
          <svg viewBox="0 0 120 40" width={100} height={34} role="img" aria-label="LUMI">
            <defs>
              <linearGradient id="sunGradC" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stopColor="#FBBF24" />
                <stop offset="50%" stopColor="#F59E0B" />
                <stop offset="100%" stopColor="#F97316" />
              </linearGradient>
            </defs>
            <g transform="translate(4, 4)">
              <line x1="0" y1="20" x2="24" y2="20" stroke="currentColor" strokeOpacity="0.5" strokeWidth="1.2" strokeLinecap="round" />
              <path d="M 4 20 A 8 8 0 0 1 20 20" fill="url(#sunGradC)" />
              <line x1="12" y1="2" x2="12" y2="7" stroke="#FBBF24" strokeWidth="1.5" strokeLinecap="round" />
              <line x1="5" y1="5" x2="7.5" y2="9" stroke="#FBBF24" strokeWidth="1.2" strokeLinecap="round" />
              <line x1="19" y1="5" x2="16.5" y2="9" stroke="#FBBF24" strokeWidth="1.2" strokeLinecap="round" />
            </g>
            <text x="34" y="22" fontFamily="system-ui, -apple-system, 'Segoe UI', sans-serif" fontSize="20" fontWeight="700" letterSpacing="3" fill="currentColor">LUMI</text>
            <text x="34" y="34" fontFamily="system-ui, -apple-system, 'Segoe UI', sans-serif" fontSize="8" fontWeight="500" letterSpacing="1" fill="#9c7cff">StayOS</text>
          </svg>
        </Link>

        {/* Right: property chip + settings icon */}
        <div className="justify-self-end flex items-center gap-2">
          {propertyName && (
            <span className="text-xs text-gray-400 bg-surface px-2 py-1 rounded-full truncate max-w-[140px]">
              {propertyName}
            </span>
          )}
          <Link
            href="/settings/"
            className="p-2 text-gray-400 hover:text-ink transition-colors"
            aria-label="Settings"
          >
            <Settings size={20} strokeWidth={1.5} />
          </Link>
        </div>
      </div>
    </header>
  );
}
