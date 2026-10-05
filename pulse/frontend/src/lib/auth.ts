// PULSE auth - thin adapter over the shared StayOS auth module (@stayos/auth).
//
// The auth implementation now lives once in shared/auth and is consumed by the
// StayOS shell, LUMI, and PULSE so all three share a single session (SSO). This
// file only initializes the shared module with PULSE's env-derived Cognito
// config and re-exports the primitives, so existing PULSE imports (AuthGuard,
// useAuth, the use* hooks, api.ts) keep working unchanged.
//
// PULSE reuses the existing LUMI Cognito user pool (Requirement 16.1); the same
// idToken is sent as the API Authorization header and validated server-side on
// every request. Session-expiry revocation (Requirement 16.5) is enforced by the
// AuthGuard's periodic check, the token-refresh timer, and authFetch's 401
// sign-out path, all built on the primitives re-exported here.

import { initAuth } from '@stayos/auth';
import { COGNITO_CLIENT_ID, COGNITO_REGION } from './constants';

// Inject PULSE's build-time Cognito configuration into the shared module. Runs on
// first import of this module (before any auth primitive is used).
initAuth({ cognitoClientId: COGNITO_CLIENT_ID, cognitoRegion: COGNITO_REGION });

export {
  signIn,
  refreshSession,
  signOut,
  getAccessToken,
  getIdToken,
  getCurrentUser,
  isAuthenticated,
  ACCESS_TOKEN_KEY,
  ID_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
} from '@stayos/auth';

// Re-exported so existing PULSE imports of `AuthUser` from '@/lib/auth' keep
// resolving. The property/identity claims PULSE reads off the decoded idToken;
// alerts are scoped server-side to these properties (Requirement 16.6).
export type { AuthTokens, AuthUser } from '@stayos/auth';
