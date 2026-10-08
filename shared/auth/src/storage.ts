// Shared browser-storage abstraction for the StayOS session.
//
// This is the mechanism that makes single sign-on work across the three StayOS
// apps. All three are served from ONE CloudFront origin (shell at `/`, LUMI at
// `/lumi`, PULSE at `/pulse`), so a single `localStorage` namespace is visible
// to all of them. Tokens are stored under a shared `stayos.` key prefix; once
// the shell writes them on login, LUMI and PULSE read the same session with no
// re-prompt.
//
// Fresh installs use localStorage for a session shared across tabs. Older
// deployed apps use sessionStorage, which survives same-tab navigation but
// is isolated per tab. Reuse an existing legacy session for the lifetime of
// the current document so a feature can be upgraded independently of the shell.
// Keep each token triple in one store; never combine two users' sessions.
//
// Every accessor is SSR-safe (guards `typeof window`) because the apps are
// static-exported and modules may be evaluated without a DOM.

import type { AuthTokens } from './types';

// Shared key namespace. All StayOS apps read/write these exact keys so the
// session is a single source of truth across the origin.
const KEY_PREFIX = 'stayos.';
export const ACCESS_TOKEN_KEY = `${KEY_PREFIX}accessToken`;
export const ID_TOKEN_KEY = `${KEY_PREFIX}idToken`;
export const REFRESH_TOKEN_KEY = `${KEY_PREFIX}refreshToken`;

let selectedStorage: Storage | undefined;

/** Keep the current document on the store selected by its existing login. */
function tokenStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  if (!selectedStorage) {
    // Select once so clearing a session cannot switch to another stored login.
    selectedStorage = window.sessionStorage.getItem(ACCESS_TOKEN_KEY) !== null
      ? window.sessionStorage
      : window.localStorage;
  }
  return selectedStorage ?? null;
}

/**
 * Read a single stored token value.
 *
 * @param key - One of the exported shared storage keys.
 * @returns The stored value, or null when absent or storage is unavailable.
 */
function readToken(key: string): string | null {
  return tokenStorage()?.getItem(key) ?? null;
}

/**
 * Persist the full token triple to the shared origin storage.
 *
 * @param tokens - The access, id, and refresh tokens to store.
 */
export function setTokens(tokens: AuthTokens): void {
  const storage = tokenStorage();
  if (!storage) return;
  storage.setItem(ACCESS_TOKEN_KEY, tokens.accessToken);
  storage.setItem(ID_TOKEN_KEY, tokens.idToken);
  storage.setItem(REFRESH_TOKEN_KEY, tokens.refreshToken);
}

/**
 * Update only the access and id tokens (used after a refresh, which does not
 * re-issue the refresh token).
 *
 * @param accessToken - The freshly issued access token.
 * @param idToken - The freshly issued id token.
 */
export function updateAccessTokens(accessToken: string, idToken: string): void {
  const storage = tokenStorage();
  if (!storage) return;
  storage.setItem(ACCESS_TOKEN_KEY, accessToken);
  storage.setItem(ID_TOKEN_KEY, idToken);
}

/**
 * Return the stored access token, or null when unset / unavailable.
 *
 * @returns The access token string or null.
 */
export function getStoredAccessToken(): string | null {
  return readToken(ACCESS_TOKEN_KEY);
}

/**
 * Return the stored id token, or null when unset / unavailable.
 *
 * @returns The id token string or null.
 */
export function getStoredIdToken(): string | null {
  return readToken(ID_TOKEN_KEY);
}

/**
 * Return the stored refresh token, or null when unset / unavailable.
 *
 * @returns The refresh token string or null.
 */
export function getStoredRefreshToken(): string | null {
  return readToken(REFRESH_TOKEN_KEY);
}

/**
 * Clear the entire StayOS session from shared storage. Removing the access
 * token key also drives the cross-tab / cross-app sign-out listeners, which
 * watch for that key being emptied.
 */
export function clearTokens(): void {
  if (typeof window === 'undefined') return;
  // A deliberate sign-out invalidates both current and legacy browser state.
  for (const storage of [window.localStorage, window.sessionStorage]) {
    storage.removeItem(ACCESS_TOKEN_KEY);
    storage.removeItem(ID_TOKEN_KEY);
    storage.removeItem(REFRESH_TOKEN_KEY);
  }
}
