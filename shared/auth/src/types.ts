// Shared auth types for the StayOS single-sign-on layer.
//
// These types are consumed by all three StayOS frontends (the StayOS shell at
// `/`, LUMI at `/lumi`, and PULSE at `/pulse`). They describe the Cognito token
// set held in browser storage and the identity claims decoded from the idToken.
// Keeping them in one place guarantees the shell and both features agree on the
// exact shape of a session.

// The Cognito token triple returned by InitiateAuth and persisted client-side.
export interface AuthTokens {
  accessToken: string;
  idToken: string;
  refreshToken: string;
}

// Identity claims the StayOS apps read off the decoded idToken. Alerts and
// briefs are scoped server-side to these properties; the client values are used
// only for display and request hints (the server validates the JWT on every
// call).
export interface AuthUser {
  email: string;
  // Full display name from the Cognito "name" claim (e.g. "Jennifer Smith").
  // Empty string when the token carries no name claim; callers fall back to email.
  name: string;
  gmAlias: string;
  propertyId: string;
}

// Runtime configuration injected by each app at startup (see initAuth). The
// values are env-derived per app (NEXT_PUBLIC_*), never hardcoded here, so the
// shared module carries no resource identifiers of its own.
export interface AuthConfig {
  // Cognito App Client ID (public SPA client, no secret).
  cognitoClientId: string;
  // AWS region hosting the Cognito user pool (e.g. "us-east-1").
  cognitoRegion: string;
}
