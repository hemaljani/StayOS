// Core client-side auth primitives shared by all StayOS apps.
//
// This is the single source of truth for StayOS authentication: the shell at
// `/`, LUMI at `/lumi`, and PULSE at `/pulse` all import these functions so they
// share one implementation and one session. It authenticates against the shared
// LUMI Cognito user pool with USER_PASSWORD_AUTH, rotates tokens via
// REFRESH_TOKEN_AUTH, decodes idToken claims for display, and gates the UI with
// isAuthenticated(). Tokens live in the shared origin storage (see storage.ts),
// which is what enables single sign-on across the three apps.
//
// Security note: the client never verifies the JWT signature - the server
// validates the token on every API call. Client-side decoding is used only for
// display and cheap UX gating.

import { getAuthConfig, getCognitoEndpoint } from './config';
import {
  clearTokens,
  getStoredAccessToken,
  getStoredIdToken,
  getStoredRefreshToken,
  setTokens,
  updateAccessTokens,
} from './storage';
import type { AuthTokens, AuthUser } from './types';

// Cognito InitiateAuth is a JSON-RPC style POST with an X-Amz-Target header.
const CONTENT_TYPE = 'application/x-amz-json-1.1';
const INITIATE_AUTH_TARGET = 'AWSCognitoIdentityProviderService.InitiateAuth';

/**
 * Decode the payload segment of a JWT without verifying its signature.
 *
 * @param token - A JWT (header.payload.signature).
 * @returns The decoded payload object.
 * @throws Error if the token is malformed or not valid base64 JSON. Callers are
 *   expected to catch and treat a throw as "no valid claims".
 */
function decodeJwtPayload(token: string): Record<string, unknown> {
  // atob is available in the browser; the apps only call these paths client-side.
  return JSON.parse(atob(token.split('.')[1]));
}

/**
 * Sign in against Cognito with StayOS credentials and persist the tokens to the
 * shared origin storage.
 *
 * @param email - The GM's email (Cognito username attribute).
 * @param password - The GM's password.
 * @returns The issued token triple.
 * @throws Error with a user-facing message when Cognito rejects the credentials
 *   (surfaced by the login form).
 */
export async function signIn(email: string, password: string): Promise<AuthTokens> {
  const { cognitoClientId } = getAuthConfig();

  // Direct email/password auth from the SPA login form (ALLOW_USER_PASSWORD_AUTH).
  const response = await fetch(getCognitoEndpoint(), {
    method: 'POST',
    headers: {
      'Content-Type': CONTENT_TYPE,
      'X-Amz-Target': INITIATE_AUTH_TARGET,
    },
    body: JSON.stringify({
      AuthFlow: 'USER_PASSWORD_AUTH',
      ClientId: cognitoClientId,
      AuthParameters: { USERNAME: email, PASSWORD: password },
    }),
  });

  if (!response.ok) {
    // Surface a credentials error so the login form can display it.
    const error = await response.json().catch(() => ({}));
    throw new Error(error.message || 'Authentication failed');
  }

  const data = await response.json();
  const result = data.AuthenticationResult;
  const tokens: AuthTokens = {
    accessToken: result.AccessToken,
    idToken: result.IdToken,
    refreshToken: result.RefreshToken,
  };

  setTokens(tokens);
  return tokens;
}

/**
 * Exchange the stored refresh token for a fresh access/id token pair.
 *
 * @returns The refreshed token triple, or null when no refresh token exists or
 *   the exchange fails (caller treats null as "session over").
 */
export async function refreshSession(): Promise<AuthTokens | null> {
  const refreshToken = getStoredRefreshToken();
  if (!refreshToken) return null;

  const { cognitoClientId } = getAuthConfig();

  try {
    // Silent token refresh keeps GMs signed in without re-entering credentials
    // (ALLOW_REFRESH_TOKEN_AUTH).
    const response = await fetch(getCognitoEndpoint(), {
      method: 'POST',
      headers: {
        'Content-Type': CONTENT_TYPE,
        'X-Amz-Target': INITIATE_AUTH_TARGET,
      },
      body: JSON.stringify({
        AuthFlow: 'REFRESH_TOKEN_AUTH',
        ClientId: cognitoClientId,
        AuthParameters: { REFRESH_TOKEN: refreshToken },
      }),
    });

    if (!response.ok) return null;

    const data = await response.json();
    const result = data.AuthenticationResult;
    updateAccessTokens(result.AccessToken, result.IdToken);
    // The refresh token is not re-issued on refresh; retain the existing one.
    return {
      accessToken: result.AccessToken,
      idToken: result.IdToken,
      refreshToken,
    };
  } catch {
    // Network or parse failure: treat as an unrefreshable session.
    return null;
  }
}

/**
 * Clear all stored tokens (sign-out / session-expiry). Emptying the access
 * token key also triggers the cross-app storage listeners to sign out.
 */
export function signOut(): void {
  clearTokens();
}

/**
 * Return the stored access token (used for SigV4 / bearer scenarios).
 *
 * @returns The access token or null.
 */
export function getAccessToken(): string | null {
  return getStoredAccessToken();
}

/**
 * Return the stored id token - the credential sent as the API Authorization
 * header.
 *
 * @returns The id token or null.
 */
export function getIdToken(): string | null {
  return getStoredIdToken();
}

/**
 * Decode the idToken claims into the AuthUser shape.
 *
 * @returns The current user's identity claims, or null when no valid token is
 *   present.
 */
export function getCurrentUser(): AuthUser | null {
  const idToken = getStoredIdToken();
  if (!idToken) return null;
  try {
    const payload = decodeJwtPayload(idToken);
    return {
      email: (payload.email as string) || '',
      name: (payload.name as string) || '',
      gmAlias: (payload['custom:gmAlias'] as string) || '',
      propertyId: (payload['custom:propertyId'] as string) || '',
    };
  } catch {
    // Malformed token: no derivable user.
    return null;
  }
}

/**
 * Whether a non-expired access token is present. Used by the app AuthGuards to
 * gate protected routes.
 *
 * @returns True when a stored access token exists and has not expired.
 */
export function isAuthenticated(): boolean {
  const token = getStoredAccessToken();
  if (!token) return false;
  try {
    const payload = decodeJwtPayload(token);
    const exp = payload.exp as number | undefined;
    if (typeof exp !== 'number') return false;
    return exp * 1000 > Date.now();
  } catch {
    // Malformed token: treat as unauthenticated.
    return false;
  }
}
