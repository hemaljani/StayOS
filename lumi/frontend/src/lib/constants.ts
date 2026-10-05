// Base path the LUMI PWA is served under. LUMI lives at /lumi/ on the shared
// StayOS CloudFront distribution (the StayOS shell owns the site root "/").
// Matches next.config.js `basePath`. next/link, next/router, and next/image
// apply this automatically; raw redirects (window.location), the service-worker
// registration/scope, the AudioWorklet module URL, and static asset URLs in
// <head> / manifest do NOT, so those must be wrapped with withBase().
export const BASE_PATH = '/lumi';

// Prefix an absolute app path with BASE_PATH for use in raw window.location
// redirects, serviceWorker.register, worklet module URLs, and hardcoded asset
// hrefs. Idempotent: a path already under BASE_PATH is returned unchanged.
// NOTE: do NOT use this for the StayOS shell root ("/") - the shell is outside
// LUMI's basePath, so redirects to it stay a raw "/".
export function withBase(path: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  if (normalized === BASE_PATH || normalized.startsWith(`${BASE_PATH}/`)) {
    return normalized;
  }
  return `${BASE_PATH}${normalized}`;
}

export const SEVERITY_COLORS = {
  URGENT: 'text-danger',
  HIGH: 'text-warning',
  MEDIUM: 'text-accent',
  LOW: 'text-gray-400',
} as const;

export const SEVERITY_BG_COLORS = {
  URGENT: 'bg-danger/20',
  HIGH: 'bg-warning/20',
  MEDIUM: 'bg-accent/20',
  LOW: 'bg-gray-400/20',
} as const;

export const TIER_COLORS = {
  AMBASSADOR: 'text-tier-ambassador',
  TITANIUM: 'text-tier-titanium',
  PLATINUM: 'text-tier-platinum',
} as const;

export const TIER_BG_COLORS = {
  AMBASSADOR: 'bg-tier-ambassador',
  TITANIUM: 'bg-tier-titanium',
  PLATINUM: 'bg-tier-platinum',
} as const;

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:3001/v1';
export const COGNITO_USER_POOL_ID = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID || '';
export const COGNITO_CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || '';
export const COGNITO_REGION = process.env.NEXT_PUBLIC_COGNITO_REGION || 'us-east-1';
export const COGNITO_IDENTITY_POOL_ID = process.env.NEXT_PUBLIC_COGNITO_IDENTITY_POOL_ID || '';
export const AGENTCORE_RUNTIME_ARN = process.env.NEXT_PUBLIC_AGENTCORE_RUNTIME_ARN || '';
// CHAT_RUNTIME_ARN is the AgentCore Runtime ARN for the text-based chat agent
// (separate runtime/deployment from the voice agent).
export const CHAT_RUNTIME_ARN = process.env.NEXT_PUBLIC_CHAT_RUNTIME_ARN || '';
// AWS_REGION is used for SigV4 signing and Identity Pool credential exchange.
// Defaults to the same region as COGNITO_REGION for single-region deployments.
export const AWS_REGION = process.env.NEXT_PUBLIC_AWS_REGION || 'us-east-1';
