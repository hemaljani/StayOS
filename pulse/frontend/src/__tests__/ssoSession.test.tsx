// Cross-context SSO tests for PULSE (shared StayOS session).
//
// Proves the single-sign-on contract from PULSE's side:
//   1. A session written to the SHARED localStorage namespace (stayos.*) by any
//      StayOS app (the shell or LUMI) is seen as authenticated by PULSE with no
//      re-login - this is the SSO handoff.
//   2. Clearing the shared access-token key (a sign-out in another app/tab) makes
//      PULSE report unauthenticated, which drives the AuthGuard revoke path.
//   3. A property: for any valid claim set stored under the shared namespace,
//      PULSE agrees the user is authenticated and resolves the same identity.
// These exercise the REAL shared auth module (no mock) so they validate the
// actual storage contract the three apps share.

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fc from 'fast-check';

import {
  isAuthenticated,
  getCurrentUser,
  ACCESS_TOKEN_KEY,
  ID_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
} from '@/lib/auth';

// Build a JWT whose payload carries the given claims (signature irrelevant).
function makeJwt(payload: Record<string, unknown>): string {
  return `${btoa(JSON.stringify({ alg: 'none' }))}.${btoa(JSON.stringify(payload))}.sig`;
}

function tokenExpiringIn(seconds: number, extra: Record<string, unknown> = {}): string {
  return makeJwt({ exp: Math.floor(Date.now() / 1000) + seconds, ...extra });
}

// Simulate a session established by another StayOS app (shell / LUMI) writing to
// the shared origin localStorage.
function seedSharedSession(claims: Record<string, unknown>): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, tokenExpiringIn(3600));
  localStorage.setItem(ID_TOKEN_KEY, tokenExpiringIn(3600, claims));
  localStorage.setItem(REFRESH_TOKEN_KEY, 'shared-refresh');
}

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe('PULSE reads the shared StayOS session (SSO handoff)', () => {
  it('is authenticated when the shell/LUMI wrote a session to the shared namespace', () => {
    expect(isAuthenticated()).toBe(false); // cold: no session
    seedSharedSession({ email: 'gm@example.com' });
    // No PULSE login occurred; PULSE sees the shared session directly.
    expect(isAuthenticated()).toBe(true);
  });

  it('resolves the current user from the shared idToken claims', () => {
    seedSharedSession({
      email: 'gm@example.com',
      'custom:gmAlias': 'ALOHA-CHI-001',
      'custom:propertyId': 'chi-001',
    });
    expect(getCurrentUser()).toEqual({
      email: 'gm@example.com',
      name: '',
      gmAlias: 'ALOHA-CHI-001',
      propertyId: 'chi-001',
    });
  });

  it('reports unauthenticated after the shared access token is cleared (cross-app sign-out)', () => {
    seedSharedSession({ email: 'gm@example.com' });
    expect(isAuthenticated()).toBe(true);
    // Another app / tab signs out -> the shared access token key is emptied.
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    expect(isAuthenticated()).toBe(false);
  });
});

describe('SSO consistency property (PULSE side)', () => {
  it('agrees on authenticated state for any valid claim set in the shared namespace', () => {
    fc.assert(
      fc.property(
        fc.record({
          email: fc.emailAddress(),
          gmAlias: fc.stringMatching(/^[A-Za-z0-9-]{1,20}$/),
          propertyId: fc.stringMatching(/^[A-Za-z0-9-]{1,20}$/),
        }),
        ({ email, gmAlias, propertyId }) => {
          localStorage.clear();
          seedSharedSession({
            email,
            'custom:gmAlias': gmAlias,
            'custom:propertyId': propertyId,
          });
          const user = getCurrentUser();
          return (
            isAuthenticated() === true &&
            user?.email === email &&
            user?.gmAlias === gmAlias &&
            user?.propertyId === propertyId
          );
        },
      ),
    );
  });
});
