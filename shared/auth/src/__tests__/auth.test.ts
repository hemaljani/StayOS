// Unit + property tests for the shared StayOS auth module.
//
// Ports and expands the PULSE auth coverage against the shared implementation:
//   - sign-in success persists tokens under the shared localStorage namespace
//   - sign-in failure surfaces a credentials error and stores nothing
//   - refreshSession null-paths (no refresh token, non-ok response, network throw)
//   - refreshSession success updates access/id but retains the refresh token
//   - getCurrentUser / isAuthenticated decode claims and handle malformed tokens
//   - clearTokens (signOut) empties the shared session
// A fast-check property asserts that for any non-expired token, all consumers
// (isAuthenticated + getCurrentUser + the shell/feature guards that build on
// them) agree the session is authenticated.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fc from 'fast-check';

import {
  initAuth,
  signIn,
  refreshSession,
  signOut,
  getCurrentUser,
  getIdToken,
  getAccessToken,
  isAuthenticated,
  ACCESS_TOKEN_KEY,
  ID_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
  setTokens,
} from '../index';

// Build a JWT with the given payload. Signature is irrelevant (never verified
// client-side); only the base64url payload segment matters.
function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: 'none', typ: 'JWT' }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.sig`;
}

// A token that expires `secondsFromNow` from now (negative = already expired).
function tokenExpiringIn(secondsFromNow: number, extra: Record<string, unknown> = {}): string {
  return makeJwt({ exp: Math.floor(Date.now() / 1000) + secondsFromNow, ...extra });
}

beforeEach(() => {
  initAuth({ cognitoClientId: 'test-client-id', cognitoRegion: 'us-east-1' });
  window.localStorage.clear();
  vi.restoreAllMocks();
});

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe('signIn', () => {
  it('persists the token triple under the shared stayos namespace on success', async () => {
    const idToken = tokenExpiringIn(3600, { email: 'gm@example.com' });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          AuthenticationResult: {
            AccessToken: tokenExpiringIn(3600),
            IdToken: idToken,
            RefreshToken: 'refresh-abc',
          },
        }),
      })),
    );

    const tokens = await signIn('gm@example.com', 'Password123');

    expect(tokens.refreshToken).toBe('refresh-abc');
    // Stored under the SHARED namespace keys - this is what makes SSO work.
    expect(window.localStorage.getItem(ID_TOKEN_KEY)).toBe(idToken);
    expect(window.localStorage.getItem(REFRESH_TOKEN_KEY)).toBe('refresh-abc');
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).not.toBeNull();
  });

  it('throws a credentials error and stores nothing on failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        json: async () => ({ message: 'Incorrect username or password.' }),
      })),
    );

    await expect(signIn('gm@example.com', 'wrong')).rejects.toThrow(
      'Incorrect username or password.',
    );
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });
});

describe('refreshSession', () => {
  it('returns null when there is no stored refresh token', async () => {
    await expect(refreshSession()).resolves.toBeNull();
  });

  it('returns null when Cognito responds non-ok', async () => {
    window.localStorage.setItem(REFRESH_TOKEN_KEY, 'refresh-abc');
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })));
    await expect(refreshSession()).resolves.toBeNull();
  });

  it('returns null when the network throws', async () => {
    window.localStorage.setItem(REFRESH_TOKEN_KEY, 'refresh-abc');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down');
      }),
    );
    await expect(refreshSession()).resolves.toBeNull();
  });

  it('updates access/id tokens but retains the existing refresh token', async () => {
    window.localStorage.setItem(REFRESH_TOKEN_KEY, 'refresh-keep');
    const newAccess = tokenExpiringIn(3600);
    const newId = tokenExpiringIn(3600, { email: 'gm@example.com' });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          AuthenticationResult: { AccessToken: newAccess, IdToken: newId },
        }),
      })),
    );

    const result = await refreshSession();

    expect(result).not.toBeNull();
    expect(result?.refreshToken).toBe('refresh-keep');
    expect(getAccessToken()).toBe(newAccess);
    expect(getIdToken()).toBe(newId);
    // Refresh token is not re-issued and must be preserved.
    expect(window.localStorage.getItem(REFRESH_TOKEN_KEY)).toBe('refresh-keep');
  });
});

describe('getCurrentUser', () => {
  it('decodes email / gmAlias / propertyId claims from the idToken', () => {
    setTokens({
      accessToken: tokenExpiringIn(3600),
      idToken: makeJwt({
        email: 'gm@example.com',
        name: 'Jennifer Smith',
        'custom:gmAlias': 'ALOHA-CHI-001',
        'custom:propertyId': 'chi-001',
      }),
      refreshToken: 'r',
    });

    expect(getCurrentUser()).toEqual({
      email: 'gm@example.com',
      name: 'Jennifer Smith',
      gmAlias: 'ALOHA-CHI-001',
      propertyId: 'chi-001',
    });
  });

  it('returns null when there is no token', () => {
    expect(getCurrentUser()).toBeNull();
  });

  it('returns null for a malformed idToken', () => {
    window.localStorage.setItem(ID_TOKEN_KEY, 'not-a-jwt');
    expect(getCurrentUser()).toBeNull();
  });
});

describe('isAuthenticated', () => {
  it('is true for a non-expired access token', () => {
    window.localStorage.setItem(ACCESS_TOKEN_KEY, tokenExpiringIn(3600));
    expect(isAuthenticated()).toBe(true);
  });

  it('is false for an expired access token', () => {
    window.localStorage.setItem(ACCESS_TOKEN_KEY, tokenExpiringIn(-10));
    expect(isAuthenticated()).toBe(false);
  });

  it('is false when no token is present', () => {
    expect(isAuthenticated()).toBe(false);
  });

  it('is false for a malformed access token', () => {
    window.localStorage.setItem(ACCESS_TOKEN_KEY, 'garbage');
    expect(isAuthenticated()).toBe(false);
  });
});

describe('signOut', () => {
  it('clears every shared session key', () => {
    setTokens({
      accessToken: tokenExpiringIn(3600),
      idToken: tokenExpiringIn(3600),
      refreshToken: 'r',
    });
    signOut();
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(window.localStorage.getItem(ID_TOKEN_KEY)).toBeNull();
    expect(window.localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
  });
});

// Property: for any valid, non-expired session written to the shared storage,
// every consumer agrees the user is authenticated and resolves the same
// identity. This is the invariant the SSO relies on across shell/LUMI/PULSE.
describe('SSO consistency property', () => {
  it('all consumers agree on a non-expired session for any claims', () => {
    fc.assert(
      fc.property(
        fc.record({
          email: fc.emailAddress(),
          gmAlias: fc.string({ minLength: 1, maxLength: 20 }).filter((s) => !/[^\x20-\x7e]/.test(s)),
          propertyId: fc.string({ minLength: 1, maxLength: 20 }).filter((s) => !/[^\x20-\x7e]/.test(s)),
          ttl: fc.integer({ min: 60, max: 86_400 }),
        }),
        ({ email, gmAlias, propertyId, ttl }) => {
          window.localStorage.clear();
          setTokens({
            accessToken: tokenExpiringIn(ttl),
            idToken: tokenExpiringIn(ttl, {
              email,
              'custom:gmAlias': gmAlias,
              'custom:propertyId': propertyId,
            }),
            refreshToken: 'r',
          });

          const user = getCurrentUser();
          return (
            isAuthenticated() === true &&
            user !== null &&
            user.email === email &&
            user.gmAlias === gmAlias &&
            user.propertyId === propertyId
          );
        },
      ),
    );
  });
});
