// Shared browser-storage abstraction for the StayOS session.
//
// This is the mechanism that makes single sign-on work across the three StayOS
// apps. All three are served from ONE CloudFront origin (shell at `/`, LUMI at
// `/lumi`, PULSE at `/pulse`), so a single `localStorage` namespace is visible
// to all of them. Tokens are stored under a shared `stayos.` key prefix; once
// the shell writes them on login, LUMI and PULSE read the same session with no
// re-prompt.
//
// Why localStorage (not sessionStorage): sessionStorage is scoped per tab AND
// per browsing context, so a full-page navigation from `/` to `/pulse/` (which
// is how the apps hand off) loses it and forces a re-login. localStorage is
// shared across the origin and survives that navigation, which is exactly the
// SSO behavior we need. The refresh token already has a 30-day lifetime, so the
// session persists until explicit sign-out or refresh expiry.
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

/**
 * Whether a browser storage context is available (guards SSR / static build).
 *
 * @returns True when running in a browser with localStorage.
 */
function hasStorage(): boolean {
  return typeof window !== 'undefined' && typeof window.localStorage !== 'undefined';
}

/**
 * Read a single stored token value.
 *
 * @param key - One of the exported shared storage keys.
 * @returns The stored value, or null when absent or storage is unavailable.
 */
function readToken(key: string): string | null {
  if (!hasStorage()) return null;
  return window.localStorage.getItem(key);
}

/**
 * Persist the full token triple to the shared origin storage.
 *
 * @param tokens - The access, id, and refresh tokens to store.
 */
export function setTokens(tokens: AuthTokens): void {
  if (!hasStorage()) return;
  window.localStorage.setItem(ACCESS_TOKEN_KEY, tokens.accessToken);
  window.localStorage.setItem(ID_TOKEN_KEY, tokens.idToken);
  window.localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refreshToken);
}

/**
 * Update only the access and id tokens (used after a refresh, which does not
 * re-issue the refresh token).
 *
 * @param accessToken - The freshly issued access token.
 * @param idToken - The freshly issued id token.
 */
export function updateAccessTokens(accessToken: string, idToken: string): void {
  if (!hasStorage()) return;
  window.localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
  window.localStorage.setItem(ID_TOKEN_KEY, idToken);
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
  if (!hasStorage()) return;
  window.localStorage.removeItem(ACCESS_TOKEN_KEY);
  window.localStorage.removeItem(ID_TOKEN_KEY);
  window.localStorage.removeItem(REFRESH_TOKEN_KEY);
}
