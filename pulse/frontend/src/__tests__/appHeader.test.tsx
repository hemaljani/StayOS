// UI tests for AppHeader's Logout control.
//
// Logout must (1) clear the Cognito session tokens and (2) hard-navigate to the
// StayOS root "/" (the shell showing BOTH LUMI and PULSE), which lives OUTSIDE
// PULSE's /pulse basePath -- so it is a raw window.location assignment to "/",
// not a next/router push (which would stay under /pulse). The header is hidden
// on the login page.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { ACCESS_TOKEN_KEY, ID_TOKEN_KEY, REFRESH_TOKEN_KEY } from '@stayos/auth';

// usePathname drives the hide-on-login behavior; default to the PULSE home.
let pathname = '/';
vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
}));

import AppHeader from '@/components/AppHeader';

describe('AppHeader logout', () => {
  beforeEach(() => {
    pathname = '/';
    // Seed the SHARED StayOS session (localStorage, stayos.* namespace) - the
    // single session used by the shell, LUMI, and PULSE.
    localStorage.setItem(ACCESS_TOKEN_KEY, 'a');
    localStorage.setItem(ID_TOKEN_KEY, 'b');
    localStorage.setItem(REFRESH_TOKEN_KEY, 'c');
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it('reveals a Logout button from the account menu on app pages', () => {
    render(<AppHeader />);
    // Logout now lives behind the account (gear) menu.
    expect(screen.queryByRole('menuitem', { name: /log out/i })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /account menu/i }));
    expect(screen.getByRole('menuitem', { name: /log out/i })).toBeInTheDocument();
  });

  it('renders a "Back to StayOS" link to the shell root without signing out', () => {
    render(<AppHeader />);
    const back = screen.getByRole('link', { name: /back to stayos/i });
    // Raw anchor to the site root (the StayOS shell, outside PULSE's /pulse basePath).
    expect(back).toHaveAttribute('href', '/');
    // Session is untouched by merely rendering the back link (no sign-out).
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBe('a');
  });

  it('is hidden on the login page', () => {
    pathname = '/login/';
    const { container } = render(<AppHeader />);
    expect(container).toBeEmptyDOMElement();
  });

  it('clears the session and redirects to the StayOS root on logout', async () => {
    // Capture the redirect target without navigating the jsdom window.
    const hrefSetter = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...originalLocation, set href(v: string) { hrefSetter(v); } },
    });

    render(<AppHeader />);
    // Open the account menu, then click Logout.
    fireEvent.click(screen.getByRole('button', { name: /account menu/i }));
    fireEvent.click(screen.getByRole('menuitem', { name: /log out/i }));

    // Tokens cleared from the shared StayOS session.
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(ID_TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    // Hard redirect to the site root (NOT /pulse/...).
    expect(hrefSetter).toHaveBeenCalledWith('/');

    Object.defineProperty(window, 'location', {
      configurable: true,
      value: originalLocation,
    });
  });
});
