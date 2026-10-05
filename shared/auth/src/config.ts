// Runtime configuration for the shared StayOS auth module.
//
// The shared auth code must not hardcode any resource identifiers (Cognito
// client ID, region). Instead each consuming app (shell, LUMI, PULSE) calls
// initAuth() once at startup with its own env-derived values, and the auth
// primitives read them back through getAuthConfig(). This keeps a single auth
// implementation while letting each static-export app inline its own
// NEXT_PUBLIC_* build-time values.

import type { AuthConfig } from './types';

// Module-level singleton holding the injected config. Undefined until initAuth
// runs; the accessor throws a clear error if a primitive is used before init.
let _config: AuthConfig | undefined;

/**
 * Initialize the shared auth module with app-specific Cognito configuration.
 *
 * Call once during app bootstrap (before any sign-in / token operation). Safe to
 * call repeatedly with the same values (idempotent for identical config).
 *
 * @param config - Cognito client ID and region for this deployment.
 */
export function initAuth(config: AuthConfig): void {
  _config = config;
}

/**
 * Return the active auth configuration.
 *
 * @returns The config supplied to initAuth.
 * @throws Error if called before initAuth (misconfiguration guard).
 */
export function getAuthConfig(): AuthConfig {
  if (!_config) {
    throw new Error(
      'StayOS auth is not initialized. Call initAuth({ cognitoClientId, cognitoRegion }) at app startup.',
    );
  }
  return _config;
}

/**
 * Build the regional Cognito Identity Provider endpoint from the active config.
 *
 * @returns The `https://cognito-idp.<region>.amazonaws.com` endpoint URL.
 * @throws Error if called before initAuth.
 */
export function getCognitoEndpoint(): string {
  const { cognitoRegion } = getAuthConfig();
  return `https://cognito-idp.${cognitoRegion}.amazonaws.com`;
}
