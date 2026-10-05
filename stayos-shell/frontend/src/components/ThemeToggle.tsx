// ThemeToggle - light/dark switch with a StayOS-wide shared preference.
//
// The preference is stored under one localStorage key on the shared StayOS
// origin, so toggling here (shell) or in LUMI/PULSE applies everywhere. Flipping
// Blue CSS-variable palette (see globals.css). Dark is the default for new users
// (light applies only when explicitly saved). The pre-hydration script in
// layout.tsx applies the saved theme before paint (no flash); this component
// keeps the button in sync and persists changes.

'use client';

import { useEffect, useState } from 'react';
import { Moon, Sun } from 'lucide-react';

// Shared across shell + LUMI + PULSE (same origin, same localStorage). Keep this
// string identical in all three apps' ThemeToggle so the preference is shared.
const THEME_KEY = 'stayos-theme';

type Theme = 'light' | 'dark';

export default function ThemeToggle({ className = '' }: { className?: string }) {
  // Start from what the pre-hydration script already applied to <html> so the
  // button label matches the rendered theme on first paint. Dark is the default
  // for new users (see the bootstrap in layout.tsx), so seed the SSR state to
  // dark; the effect below reconciles with the actual <html> class after mount.
  const [theme, setTheme] = useState<Theme>('dark');

  useEffect(() => {
    const isDark = document.documentElement.classList.contains('dark');
    setTheme(isDark ? 'dark' : 'light');
  }, []);

  const toggle = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    const root = document.documentElement;
    if (next === 'dark') {
      root.classList.add('dark');
    } else {
      root.classList.remove('dark');
    }
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // localStorage may be unavailable (private mode); the in-session toggle
      // still works, the preference just will not persist.
    }
  };

  const isDark = theme === 'dark';

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      className={`flex h-9 w-9 items-center justify-center rounded-lg border border-line bg-surface text-ink-soft transition-colors hover:text-ink hover:border-accent/50 focus:outline-none focus:ring-2 focus:ring-accent ${className}`}
    >
      {isDark ? <Sun size={17} aria-hidden /> : <Moon size={17} aria-hidden />}
    </button>
  );
}
