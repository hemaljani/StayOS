// Shared constants for the PULSE PWA: environment-derived configuration and the
// Tailwind class maps for alert tiers and statuses. Centralizing the tier/status
// styling here keeps the alert cards, tier filter, and triage modal visually
// consistent and avoids scattering color literals across components.
//
// Env vars follow the NEXT_PUBLIC_* convention (mirroring LUMI) so they are
// inlined into the static export at build time. PULSE reuses the existing LUMI
// Cognito user pool (Requirement 16.1).

import type { AlertTier, VipTier } from './types';

// Base path the PULSE PWA is served under. PULSE lives at /pulse/ on the shared
// StayOS (LUMI) CloudFront distribution (Option A), matching next.config.js
// `basePath`. next/link and router.push apply this automatically; raw redirects
// (window.location), the service-worker registration/scope, and static asset
// URLs in <head> do NOT, so they must be wrapped with withBase().
export const BASE_PATH = '/pulse';

// Prefix an absolute app path with BASE_PATH for use in raw window.location
// redirects, serviceWorker.register, and hardcoded asset hrefs. Idempotent:
// a path already under BASE_PATH is returned unchanged.
export function withBase(path: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  if (normalized === BASE_PATH || normalized.startsWith(`${BASE_PATH}/`)) {
    return normalized;
  }
  return `${BASE_PATH}${normalized}`;
}

// Base URL of the PULSE REST API (API Gateway stage, ends in /v1). Falls back to
// a local dev endpoint when unset so the app runs without a build-time value.
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:3001/v1';

// Cognito configuration (shared LUMI user pool). Region defaults to us-east-1.
export const COGNITO_CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || '';
export const COGNITO_REGION = process.env.NEXT_PUBLIC_COGNITO_REGION || 'us-east-1';

// AWS region (used for realtime/regional configuration). Defaults to the Cognito
// region. The realtime WebSocket authorizes with the Cognito JWT (no SigV4).
export const AWS_REGION = process.env.NEXT_PUBLIC_AWS_REGION || COGNITO_REGION;

// Text color per alert tier.
export const TIER_TEXT_COLORS: Record<AlertTier, string> = {
  CRITICAL: 'text-tier-critical',
  WARNING: 'text-tier-warning',
  INFO: 'text-tier-info',
};

// Left-border accent per alert tier (used on alert cards).
export const TIER_BORDER_COLORS: Record<AlertTier, string> = {
  CRITICAL: 'border-l-tier-critical',
  WARNING: 'border-l-tier-warning',
  INFO: 'border-l-tier-info',
};

// Pill background + text per alert tier (used on tier pills and filters).
export const TIER_PILL_COLORS: Record<AlertTier, string> = {
  CRITICAL: 'bg-tier-critical/15 text-tier-critical',
  WARNING: 'bg-tier-warning/15 text-tier-warning',
  INFO: 'bg-tier-info/15 text-tier-info',
};

// Human-readable tier label.
export const TIER_LABELS: Record<AlertTier, string> = {
  CRITICAL: 'Critical',
  WARNING: 'Warning',
  INFO: 'Info',
};


// VIP loyalty-tier styling (Task 21.2). The prototype styles each tier with a
// distinct avatar gradient and pill; these token maps keep the VIP cards and
// profile modal consistent. AMBASSADOR uses warning gold, PLATINUM the accent
// blue/violet, TITANIUM a neutral slate.
export const VIP_TIER_ORDER: VipTier[] = ['AMBASSADOR', 'TITANIUM', 'PLATINUM'];

// Avatar gradient background per tier (Tailwind classes). Coastal Blue tiers:
// AMBASSADOR warm gold, TITANIUM slate, PLATINUM accent blue.
export const VIP_TIER_AVATAR_COLORS: Record<string, string> = {
  AMBASSADOR: 'bg-gradient-to-br from-tier-ambassador to-[#a56f1f]',
  TITANIUM: 'bg-gradient-to-br from-tier-titanium to-[#6f7885]',
  PLATINUM: 'bg-gradient-to-br from-tier-platinum to-accent-deep',
};

// Tier pill background + text per tier.
export const VIP_TIER_PILL_COLORS: Record<string, string> = {
  AMBASSADOR: 'bg-tier-ambassador/15 text-tier-ambassador',
  TITANIUM: 'bg-tier-titanium/20 text-tier-titanium',
  PLATINUM: 'bg-tier-platinum/15 text-tier-platinum',
};

// Human-readable tier label (e.g. AMBASSADOR -> "Ambassador").
export function vipTierLabel(tier: VipTier): string {
  const raw = String(tier);
  return raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
}

// Fallback avatar/pill styling for an unexpected tier value.
export const VIP_TIER_FALLBACK_AVATAR = 'bg-gradient-to-br from-ink-faint to-ink-soft';
export const VIP_TIER_FALLBACK_PILL = 'bg-surface-2 text-ink-soft';
