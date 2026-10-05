// Public entry point for the shared StayOS auth module (@stayos/auth).
//
// Consumed via a build-time TypeScript path alias by all three StayOS
// frontends (shell, LUMI, PULSE). Each app calls initAuth() once at startup
// with its env-derived Cognito config, then uses these primitives for sign-in,
// refresh, sign-out, and UI gating. See auth.ts / storage.ts / config.ts for
// the single-sign-on design (one localStorage namespace across the origin).

export { initAuth, getAuthConfig, getCognitoEndpoint } from './config';

export {
  signIn,
  refreshSession,
  signOut,
  getAccessToken,
  getIdToken,
  getCurrentUser,
  isAuthenticated,
} from './auth';

export {
  ACCESS_TOKEN_KEY,
  ID_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
  setTokens,
  updateAccessTokens,
  getStoredAccessToken,
  getStoredIdToken,
  getStoredRefreshToken,
  clearTokens,
} from './storage';

export type { AuthTokens, AuthUser, AuthConfig } from './types';
