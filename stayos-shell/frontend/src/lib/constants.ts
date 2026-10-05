// Environment-derived configuration for the StayOS shell.
//
// NEXT_PUBLIC_* values are inlined into the static export at build time. The
// shell authenticates against the shared StayOS (LUMI) Cognito user pool, so
// these MUST match the client ID and region used by LUMI and PULSE for the
// issued session to be valid across all three apps.

// Cognito App Client ID (shared LUMI public SPA client).
export const COGNITO_CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || '';

// AWS region hosting the Cognito user pool. Defaults to us-east-1.
export const COGNITO_REGION = process.env.NEXT_PUBLIC_COGNITO_REGION || 'us-east-1';
