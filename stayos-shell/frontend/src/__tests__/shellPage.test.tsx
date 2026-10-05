// Component tests for the StayOS shell root page.
//
// Covers the shell's two views and the SSO handoff contract:
//   - Unauthenticated: renders the login form; invalid credentials surface a
//     visible error and the view does NOT switch to the grid.
//   - Successful login: flips in place to the feature grid (no redirect).
//   - Authenticated on mount (shared session already present): renders the grid
//     directly - the SSO entry case.
//   - The grid links LUMI -> /lumi/ and PULSE -> /pulse/ (the launcher targets).
// The Cognito boundary (signIn) and the shared session state (isAuthenticated /
// getCurrentUser) are mocked so no real network or storage is required.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { isAuthenticated, signIn, getCurrentUser } from '@/lib/auth';

// Mock the shared auth primitives the shell consumes.
vi.mock('@/lib/auth', () => ({
  isAuthenticated: vi.fn(),
  signIn: vi.fn(),
  signOut: vi.fn(),
  getCurrentUser: vi.fn(() => null),
}));

import ShellPage from '@/app/page';

const mockedIsAuthenticated = vi.mocked(isAuthenticated);
const mockedSignIn = vi.mocked(signIn);
const mockedGetCurrentUser = vi.mocked(getCurrentUser);

beforeEach(() => {
  vi.clearAllMocks();
  mockedGetCurrentUser.mockReturnValue(null);
});

afterEach(() => cleanup());

describe('StayOS shell - unauthenticated', () => {
  it('renders the marketing landing page when there is no session', async () => {
    mockedIsAuthenticated.mockReturnValue(false);
    render(<ShellPage />);
    // Landing page hero + live-feature story, not the login form.
    expect(await screen.findByRole('heading', { name: /live today/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'LUMI' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'PULSE' })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Email')).not.toBeInTheDocument();
  });

  it('opens a popup with the feature detail when a feature panel is tapped', async () => {
    mockedIsAuthenticated.mockReturnValue(false);
    render(<ShellPage />);

    // LUMI's full description is not on the page until its panel is tapped
    // (keeps the landing uncrowded); no dialog is open initially.
    expect(await screen.findByRole('heading', { name: 'LUMI' })).toBeInTheDocument();
    expect(screen.queryByText(/walks the floor already knowing the day/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // Tapping the LUMI panel opens a popup dialog with the full description.
    fireEvent.click(screen.getByRole('button', { name: /LUMI Start the day informed/i }));
    const dialog = await screen.findByRole('dialog', { name: /LUMI details/i });
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText(/walks the floor already knowing the day/i)).toBeInTheDocument();
    // The popup is self-contained: it also shows LUMI's KPI data preview.
    expect(within(dialog).getByText('Occupancy')).toBeInTheDocument();
    expect(within(dialog).getByText('87%')).toBeInTheDocument();

    // Closing the popup removes it.
    fireEvent.click(screen.getByRole('button', { name: /^close$/i }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('the popup CTA signs in (opens the login form)', async () => {
    mockedIsAuthenticated.mockReturnValue(false);
    render(<ShellPage />);

    // Open the LUMI popup, then click its "Sign in to open LUMI" CTA.
    fireEvent.click(await screen.findByRole('button', { name: /LUMI Start the day informed/i }));
    fireEvent.click(await screen.findByRole('button', { name: /sign in to open LUMI/i }));

    // The shell swaps to the login form (the popup is dismissed).
    expect(screen.getByPlaceholderText('Email')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('reveals the login form when a Sign In call-to-action is clicked', async () => {
    mockedIsAuthenticated.mockReturnValue(false);
    render(<ShellPage />);
    fireEvent.click((await screen.findAllByRole('button', { name: /sign in/i }))[0]);
    expect(screen.getByPlaceholderText('Email')).toBeInTheDocument();
  });

  it('shows a visible error and stays on login when credentials are invalid', async () => {
    mockedIsAuthenticated.mockReturnValue(false);
    mockedSignIn.mockRejectedValue(new Error('Incorrect username or password.'));

    render(<ShellPage />);
    // Enter the login view first.
    fireEvent.click((await screen.findAllByRole('button', { name: /sign in/i }))[0]);
    fireEvent.change(screen.getByPlaceholderText('Email'), {
      target: { value: 'gm@example.com' },
    });
    fireEvent.change(screen.getByPlaceholderText('Password'), {
      target: { value: 'wrong' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Incorrect username or password.');
    });
    // Did not switch to the grid.
    expect(screen.queryByText('Available')).not.toBeInTheDocument();
  });

  it('flips to the feature grid after a successful login (no redirect)', async () => {
    mockedIsAuthenticated.mockReturnValue(false);
    mockedSignIn.mockResolvedValue({ accessToken: 'a', idToken: 'b', refreshToken: 'c' });
    mockedGetCurrentUser.mockReturnValue({
      email: 'gm@example.com',
      name: 'Jennifer Smith',
      gmAlias: 'ALOHA-CHI-001',
      propertyId: 'chi-001',
    });

    render(<ShellPage />);
    // Enter the login view first.
    fireEvent.click((await screen.findAllByRole('button', { name: /sign in/i }))[0]);
    fireEvent.change(screen.getByPlaceholderText('Email'), {
      target: { value: 'gm@example.com' },
    });
    fireEvent.change(screen.getByPlaceholderText('Password'), {
      target: { value: 'Password123' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }));

    // LUMI + PULSE launcher cards appear once authenticated.
    expect(await screen.findByRole('link', { name: 'LUMI' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'PULSE' })).toBeInTheDocument();
  });
});

describe('StayOS shell - authenticated on mount (SSO entry)', () => {
  it('renders the feature grid directly when a shared session already exists', async () => {
    mockedIsAuthenticated.mockReturnValue(true);
    mockedGetCurrentUser.mockReturnValue({
      email: 'gm@example.com',
      name: 'Jennifer Smith',
      gmAlias: 'ALOHA-CHI-001',
      propertyId: 'chi-001',
    });

    render(<ShellPage />);

    // No login form; the launcher is shown with the correct targets.
    const lumi = await screen.findByRole('link', { name: 'LUMI' });
    const pulse = screen.getByRole('link', { name: 'PULSE' });
    expect(lumi).toHaveAttribute('href', '/lumi/');
    expect(pulse).toHaveAttribute('href', '/pulse/');
    expect(screen.queryByPlaceholderText('Email')).not.toBeInTheDocument();
  });
});
