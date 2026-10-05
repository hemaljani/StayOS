// LUMI auth - thin adapter over the shared StayOS auth module (@stayos/auth).
//
// The auth implementation now lives once in shared/auth and is consumed by the
// StayOS shell, LUMI, and PULSE so all three share a single session (SSO). This
// file only initializes the shared module with LUMI's env-derived Cognito config
// and re-exports the primitives, so existing LUMI imports (AuthGuard, useAuth,
// api.ts) keep working unchanged.

import { initAuth } from '@stayos/auth';
import { COGNITO_CLIENT_ID, COGNITO_REGION } from './constants';

// Inject LUMI's build-time Cognito configuration into the shared module. Runs on
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

export type { AuthTokens, AuthUser } from '@stayos/auth';
