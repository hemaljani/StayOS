// UI tests for Task 21.7 - auth guards (Requirement 16.2-16.5).
//
// Covers the client-side auth gating:
//   - AuthGuard renders protected children when the session is authenticated.
//   - AuthGuard blocks protected content and redirects to /login when the session
//     is not authenticated (denies tabs / revokes access, Requirement 16.2/16.5).
//   - The login form rejects invalid credentials with a visible error indication
//     and does not navigate away (Requirement 16.4).
// isAuthenticated/signIn and next navigation are mocked so no real Cognito call
// or route change occurs.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { isAuthenticated, signIn, signOut } from '@/lib/auth';

// Mock next navigation: protected path for the guard; a no-op router for login.
vi.mock('next/navigation', () => ({
  usePathname: () => '/',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

// Mock the auth primitives the guard and useAuth depend on.
vi.mock('@/lib/auth', () => ({
  isAuthenticated: vi.fn(),
  signIn: vi.fn(),
  signOut: vi.fn(),
  getCurrentUser: vi.fn(() => null),
}));

import AuthGuard from '@/components/AuthGuard';
import LoginPage from '@/app/login/page';

const mockedIsAuthenticated = vi.mocked(isAuthenticated);
const mockedSignIn = vi.mocked(signIn);
const mockedSignOut = vi.mocked(signOut);

// Stub window.location so the guard's redirect is observable and does not throw.
const originalLocation = window.location;

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { href: '', assign: vi.fn(), replace: vi.fn() },
  });
});

afterEach(() => {
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: originalLocation,
  });
});

describe('AuthGuard gating (Requirement 16.2, 16.5)', () => {
  it('renders protected children when authenticated', async () => {
    mockedIsAuthenticated.mockReturnValue(true);

    render(
      <AuthGuard>
        <div>protected content</div>
      </AuthGuard>
    );

    expect(await screen.findByText('protected content')).toBeInTheDocument();
  });

  it('blocks protected content and redirects to /login when not authenticated', async () => {
    mockedIsAuthenticated.mockReturnValue(false);

    render(
      <AuthGuard>
        <div>protected content</div>
      </AuthGuard>
    );

    // Children are never rendered; access is revoked and redirected to the
    // StayOS shell login at the site root ("/"), which owns login now. "/" is
    // outside PULSE's /pulse basePath, so it is a raw redirect (not withBase).
    expect(screen.queryByText('protected content')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(mockedSignOut).toHaveBeenCalled();
      expect(window.location.href).toBe('/');
    });
  });
});

describe('Login credential rejection (Requirement 16.4)', () => {
  it('shows a visible error and does not navigate when credentials are invalid', async () => {
    mockedIsAuthenticated.mockReturnValue(false);
    mockedSignIn.mockRejectedValue(new Error('Incorrect username or password.'));

    render(<LoginPage />);

    fireEvent.change(screen.getByPlaceholderText('Email'), {
      target: { value: 'gm@example.com' },
    });
    fireEvent.change(screen.getByPlaceholderText('Password'), {
      target: { value: 'wrong-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Sign In/ }));

    // The credentials error is surfaced in the alert region...
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Incorrect username or password.');
    });
    // ...and the failed sign-in did not navigate away from the login page.
    expect(window.location.href).toBe('');
  });
});
