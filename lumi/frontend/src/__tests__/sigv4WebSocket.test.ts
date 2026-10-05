/**
 * Unit tests for the SigV4 presigned WebSocket URL generator.
 *
 * Validates that generatePresignedWsUrl produces correctly structured
 * presigned URLs with all required SigV4 query parameters for AgentCore
 * WebSocket authentication.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { generatePresignedWsUrl } from '@/lib/sigv4WebSocket';
import type { AwsCredentials } from '@/lib/identityPool';

const TEST_RUNTIME_ARN =
  'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/stayos-voice-agent-xyz';
const TEST_REGION = 'us-east-1';
const TEST_SESSION_ID = '550e8400-e29b-41d4-a716-446655440000';

const TEST_CREDENTIALS: AwsCredentials = {
  accessKeyId: 'AKIAIOSFODNN7EXAMPLE',
  secretAccessKey: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
  sessionToken: 'FwoGZXIvYXdzEBYaDHqa0AP/HgTest+SessionToken==',
  expiration: new Date('2025-01-15T13:00:00Z'),
};

describe('generatePresignedWsUrl', () => {
  beforeEach(() => {
    // Fix the date so SigV4 signing produces deterministic output
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2025-01-15T12:30:45Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns a wss:// URL targeting the AgentCore endpoint', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    expect(url).toMatch(/^wss:\/\/bedrock-agentcore\.us-east-1\.amazonaws\.com\//);
  });

  it('includes the URL-encoded runtime ARN in the path', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const encodedArn = encodeURIComponent(TEST_RUNTIME_ARN);
    expect(url).toContain(`/runtimes/${encodedArn}/ws`);
  });

  it('includes the X-Amz-Algorithm query parameter', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    expect(params.get('X-Amz-Algorithm')).toBe('AWS4-HMAC-SHA256');
  });

  it('includes the X-Amz-Credential with correct scope', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    const credential = params.get('X-Amz-Credential');
    expect(credential).toBe(
      'AKIAIOSFODNN7EXAMPLE/20250115/us-east-1/bedrock-agentcore/aws4_request'
    );
  });

  it('includes the X-Amz-Date matching the fixed timestamp', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    expect(params.get('X-Amz-Date')).toBe('20250115T123045Z');
  });

  it('sets X-Amz-Expires to 300 seconds', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    expect(params.get('X-Amz-Expires')).toBe('300');
  });

  it('includes X-Amz-SignedHeaders as host', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    expect(params.get('X-Amz-SignedHeaders')).toBe('host');
  });

  it('includes the X-Amz-Security-Token from credentials', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    expect(params.get('X-Amz-Security-Token')).toBe(TEST_CREDENTIALS.sessionToken);
  });

  it('includes a non-empty X-Amz-Signature', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    const signature = params.get('X-Amz-Signature');
    expect(signature).toBeTruthy();
    // SigV4 signatures are 64-char lowercase hex strings
    expect(signature).toMatch(/^[a-f0-9]{64}$/);
  });

  it('includes the session ID query parameter', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    expect(params.get('X-Amzn-Bedrock-AgentCore-Runtime-Session-Id')).toBe(
      TEST_SESSION_ID
    );
  });

  it('generates a random session ID when none is provided', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION
    );

    const params = new URL(url.replace('wss://', 'https://')).searchParams;
    const sessionId = params.get('X-Amzn-Bedrock-AgentCore-Runtime-Session-Id');
    // UUID v4 format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
    expect(sessionId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
    );
  });

  it('produces a deterministic signature for the same inputs', async () => {
    const url1 = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );
    const url2 = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      TEST_REGION,
      TEST_SESSION_ID
    );

    expect(url1).toBe(url2);
  });

  it('produces different signatures for different regions', async () => {
    const urlEast = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      'us-east-1',
      TEST_SESSION_ID
    );
    const urlWest = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      'us-west-2',
      TEST_SESSION_ID
    );

    const paramsEast = new URL(urlEast.replace('wss://', 'https://')).searchParams;
    const paramsWest = new URL(urlWest.replace('wss://', 'https://')).searchParams;
    expect(paramsEast.get('X-Amz-Signature')).not.toBe(
      paramsWest.get('X-Amz-Signature')
    );
  });

  it('uses the correct host for different regions', async () => {
    const url = await generatePresignedWsUrl(
      TEST_RUNTIME_ARN,
      TEST_CREDENTIALS,
      'eu-west-1',
      TEST_SESSION_ID
    );

    expect(url).toMatch(/^wss:\/\/bedrock-agentcore\.eu-west-1\.amazonaws\.com\//);
  });
});
