// Verify OAuth wire format and that credentials cannot leak to an arbitrary URL.
import { afterEach, expect, it, vi } from 'vitest';
import { openAgentWebSocket } from '@/lib/agentWebSocket';

afterEach(() => vi.unstubAllGlobals());

it('uses OAuth subprotocols with a fresh session and no token in the URL', () => {
  const create = vi.fn(function () {});
  vi.stubGlobal('WebSocket', create);
  const arn = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-example';
  openAgentWebSocket(arn, 'test.access.token', 'us-east-1');
  const [url, protocols] = create.mock.calls[0] as unknown as [string, string[]];
  expect(url).toContain(encodeURIComponent(arn));
  expect(url).toContain('X-Amzn-Bedrock-AgentCore-Runtime-Session-Id=');
  expect(url).not.toContain('test.access.token');
  expect(protocols).toEqual([
    `base64UrlBearerAuthorization.${btoa('test.access.token').replace(/=+$/, '')}`,
    'base64UrlBearerAuthorization',
  ]);
});

it.each([
  ['https://example.invalid', 'token', 'us-east-1'],
  ['arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-example', 'token', 'us-west-2'],
  ['arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-example', '', 'us-east-1'],
])('rejects invalid destination or missing credentials', (arn, token, region) => {
  const create = vi.fn();
  vi.stubGlobal('WebSocket', create);
  expect(() => openAgentWebSocket(arn, token, region)).toThrow();
  expect(create).not.toHaveBeenCalled();
});
