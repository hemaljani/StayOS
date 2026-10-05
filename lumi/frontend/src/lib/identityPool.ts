/**
 * Cognito Identity Pool credential helper for AgentCore WebSocket authentication.
 *
 * Exchanges a Cognito User Pool ID Token for temporary AWS credentials
 * via the Cognito Identity Pool. These credentials enable the browser to
 * generate SigV4-presigned WebSocket URLs for the AgentCore endpoint.
 *
 * Flow:
 *   1. GetId - obtain an identity ID from the Identity Pool using the ID token
 *   2. GetCredentialsForIdentity - exchange the identity for temporary AWS creds
 *   3. Return { accessKeyId, secretAccessKey, sessionToken, expiration }
 */

import {
  CognitoIdentityClient,
  GetIdCommand,
  GetCredentialsForIdentityCommand,
} from '@aws-sdk/client-cognito-identity';
import { COGNITO_IDENTITY_POOL_ID } from '@/lib/constants';
import { COGNITO_USER_POOL_ID, COGNITO_REGION } from '@/lib/constants';

/**
 * Temporary AWS credentials obtained from the Cognito Identity Pool.
 * Used by the SigV4 presigning utility to authenticate WebSocket connections.
 */
export interface AwsCredentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
  expiration: Date;
}

/**
 * The provider key format for Cognito User Pool federation with Identity Pools.
 * Format: cognito-idp.<region>.amazonaws.com/<userPoolId>
 */
const COGNITO_PROVIDER_KEY = `cognito-idp.${COGNITO_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}`;

/**
 * CognitoIdentityClient configured for the deployment region.
 * Initialized at module level for reuse across calls within the same session.
 */
const identityClient = new CognitoIdentityClient({ region: COGNITO_REGION });

/**
 * Exchanges a Cognito User Pool ID Token for temporary AWS credentials
 * via the Cognito Identity Pool.
 *
 * The returned credentials grant the authenticated Identity Pool role,
 * which has permission to invoke AgentCore WebSocket streams (SigV4).
 *
 * @param idToken - The Cognito User Pool ID Token (JWT) from the current session.
 * @returns Temporary AWS credentials for SigV4 signing.
 * @throws Error if the credential exchange fails (invalid token, pool misconfiguration).
 */
export async function getIdentityPoolCredentials(
  idToken: string
): Promise<AwsCredentials> {
  // Map the Cognito User Pool provider to the ID token for authenticated identity
  const logins: Record<string, string> = {
    [COGNITO_PROVIDER_KEY]: idToken,
  };

  // Step 1: Get an identity ID from the Identity Pool
  let identityId: string;
  try {
    const getIdResponse = await identityClient.send(
      new GetIdCommand({
        IdentityPoolId: COGNITO_IDENTITY_POOL_ID,
        Logins: logins,
      })
    );

    if (!getIdResponse.IdentityId) {
      throw new Error('Identity Pool returned no IdentityId');
    }
    identityId = getIdResponse.IdentityId;
  } catch (error: unknown) {
    const message =
      error instanceof Error ? error.message : 'Unknown error';
    throw new Error(
      `Failed to obtain Identity Pool identity: ${message}. Please log in again.`
    );
  }

  // Step 2: Exchange the identity for temporary AWS credentials
  try {
    const credentialsResponse = await identityClient.send(
      new GetCredentialsForIdentityCommand({
        IdentityId: identityId,
        Logins: logins,
      })
    );

    const credentials = credentialsResponse.Credentials;

    if (
      !credentials?.AccessKeyId ||
      !credentials?.SecretKey ||
      !credentials?.SessionToken
    ) {
      throw new Error('Identity Pool returned incomplete credentials');
    }

    return {
      accessKeyId: credentials.AccessKeyId,
      secretAccessKey: credentials.SecretKey,
      sessionToken: credentials.SessionToken,
      expiration: credentials.Expiration ?? new Date(Date.now() + 3600 * 1000),
    };
  } catch (error: unknown) {
    // Re-throw if it's already our formatted error
    if (error instanceof Error && error.message.includes('incomplete credentials')) {
      throw error;
    }
    const message =
      error instanceof Error ? error.message : 'Unknown error';
    throw new Error(
      `Failed to obtain AWS credentials from Identity Pool: ${message}. Please log in again.`
    );
  }
}
