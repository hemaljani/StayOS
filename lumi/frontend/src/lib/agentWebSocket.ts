// Connect browser sessions with the same Cognito token used by agent identity.
// AgentCore validates OAuth in the handshake; tokens stay out of URL query logs.

export function openAgentWebSocket(
  runtimeArn: string,
  accessToken: string,
  region: string
): WebSocket {
  if (!accessToken) throw new Error('Please sign in before connecting.');
  const arn = /^arn:aws:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime\/[A-Za-z0-9_-]+$/.exec(runtimeArn);
  if (!arn || arn[1] !== region) throw new Error('Agent service is not configured.');

  const encoded = btoa(accessToken)
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const session = crypto.randomUUID();
  const url = `wss://bedrock-agentcore.${region}.amazonaws.com/runtimes/${encodeURIComponent(runtimeArn)}/ws`
    + `?X-Amzn-Bedrock-AgentCore-Runtime-Session-Id=${encodeURIComponent(session)}`;
  return new WebSocket(url, [
    `base64UrlBearerAuthorization.${encoded}`,
    'base64UrlBearerAuthorization',
  ]);
}
