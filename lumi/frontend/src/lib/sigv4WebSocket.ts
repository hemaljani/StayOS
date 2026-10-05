/**
 * SigV4 presigned WebSocket URL generator for AgentCore Runtime.
 *
 * Uses the official @aws-sdk/signature-v4 package (same signing logic as
 * botocore's SigV4QueryAuth) to generate a presigned WebSocket URL.
 * This ensures byte-for-byte canonical request compatibility with the
 * AgentCore service, avoiding subtle encoding bugs in hand-rolled signing.
 *
 * The presigned URL targets the AgentCore WebSocket endpoint:
 *   wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<encodedArn>/ws
 */

import { SignatureV4 } from '@aws-sdk/signature-v4';
import { Sha256 } from '@aws-crypto/sha256-browser';
import { HttpRequest } from '@smithy/protocol-http';
import type { AwsCredentials } from './identityPool';

/** Service name for AgentCore SigV4 signing scope. */
const SERVICE = 'bedrock-agentcore';

/** Presigned URL expiration in seconds (AgentCore max: 300). */
const PRESIGNED_URL_EXPIRES_SECONDS = 300;

/**
 * Generates a SigV4-presigned WebSocket URL for the AgentCore Runtime endpoint.
 *
 * Uses @aws-sdk/signature-v4 SignatureV4.presign() which handles canonical
 * request construction, path encoding, query string sorting, and HMAC
 * derivation identically to botocore — eliminating signing mismatches.
 *
 * @param runtimeArn - Full ARN of the AgentCore Runtime.
 * @param credentials - Temporary AWS credentials from the Cognito Identity Pool.
 * @param region - AWS region (e.g., "us-east-1").
 * @param sessionId - Optional session ID for AgentCore session routing.
 * @returns The full presigned WebSocket URL (wss:// scheme).
 */
export async function generatePresignedWsUrl(
  runtimeArn: string,
  credentials: AwsCredentials,
  region: string,
  sessionId?: string
): Promise<string> {
  const resolvedSessionId = sessionId ?? crypto.randomUUID();
  const host = `bedrock-agentcore.${region}.amazonaws.com`;

  // URL-encode the ARN for the path segment (matching SDK behavior: quote(arn, safe=""))
  const encodedArn = encodeURIComponent(runtimeArn);
  const path = `/runtimes/${encodedArn}/ws`;

  // Create the signer with the same config as botocore's SigV4QueryAuth
  const signer = new SignatureV4({
    service: SERVICE,
    region,
    credentials: {
      accessKeyId: credentials.accessKeyId,
      secretAccessKey: credentials.secretAccessKey,
      sessionToken: credentials.sessionToken,
    },
    sha256: Sha256,
  });

  // Build an HTTP request object that SignatureV4 can presign
  const request = new HttpRequest({
    method: 'GET',
    protocol: 'wss:',
    hostname: host,
    port: 443,
    path,
    query: {
      'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': resolvedSessionId,
    },
    headers: {
      host,
    },
  });

  // Presign the request — this adds X-Amz-* query params with the signature
  const presigned = await signer.presign(request, {
    expiresIn: PRESIGNED_URL_EXPIRES_SECONDS,
    // Use UNSIGNED-PAYLOAD for WebSocket presigned URLs
    unsignableHeaders: new Set(),
    unhoistableHeaders: new Set(),
  });

  // Reconstruct the full URL from the presigned request
  const queryString = Object.entries(presigned.query as Record<string, string>)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
    .join('&');

  return `wss://${host}${path}?${queryString}`;
}
