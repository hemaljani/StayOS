// Presentation helpers for the PULSE PWA.
//
// Small pure formatters shared by the alert feed and triage modal so display
// logic (relative timestamps, tier icon selection, alert-type labels) is defined
// once and stays consistent. Kept free of React so it is trivially testable.

import { AlertOctagon, AlertTriangle, Info, type LucideIcon } from 'lucide-react';
import type { Alert, AlertTier, AlertType } from './types';

// The PULSE tier-filter selection: a specific tier or the catch-all "ALL". Kept
// here (mirroring TierFilter's TierFilterValue) so the pure filter helper below
// stays free of any React component import.
export type TierSelection = 'ALL' | AlertTier;

// Map each tier to its lucide icon (used on alert cards and the triage banner).
const TIER_ICONS: Record<AlertTier, LucideIcon> = {
  CRITICAL: AlertOctagon,
  WARNING: AlertTriangle,
  INFO: Info,
};

// Return the icon component for an alert tier.
export function tierIcon(tier: AlertTier): LucideIcon {
  return TIER_ICONS[tier];
}

// Human-readable label for an alert type (e.g. WALK_RISK -> "Walk Risk"). Auto-
// humanizes from the enum, so new types (e.g. FORECAST_OVERSELL -> "Forecast
// Oversell") get a sensible label with no per-type entry needed.
export function alertTypeLabel(type: AlertType): string {
  return type
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

// Optional per-type glyph overlay (Requirement 4.7). Most alert types rely on
// their tier icon; forward-looking heads-ups get a distinct anticipatory glyph
// so a forecast reads as a prediction rather than a live incident. Keyed
// partially: only types that warrant a special marker appear here.
const ALERT_TYPE_GLYPHS: Partial<Record<AlertType, string>> = {
  // Telescope: a look-ahead / predictive marker for the forecast oversell heads-up.
  FORECAST_OVERSELL: '🔭',
};

// Return the anticipatory glyph for an alert type, or undefined when the type
// has no special marker (callers fall back to the tier icon).
export function alertTypeGlyph(type: AlertType): string | undefined {
  return ALERT_TYPE_GLYPHS[type];
}

// Format an ISO 8601 timestamp as a compact relative string (e.g. "4 min ago").
// Falls back to a locale time string for spans beyond a day, and to an empty
// string when the timestamp is missing or unparseable.
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';

  const diffMs = Date.now() - then;
  const diffMin = Math.floor(diffMs / 60000);

  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin} min ago`;

  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} hr ago`;

  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

// Format an ISO 8601 timestamp as a short local clock time (e.g. "9:47 AM").
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Whether an alert should appear in the live feed. Property 20 / Requirement
// 12.4: RESOLVED alerts are excluded from the live feed and shown only in the
// resolved history section.
export function isLiveAlert(alert: Alert): boolean {
  return alert.status !== 'RESOLVED';
}

// Partition an alert list into the live feed and the resolved history. The two
// views are disjoint by status: a RESOLVED alert appears only in `resolved` and
// never in `live`, and every non-RESOLVED alert appears only in `live` (Property
// 20 / Requirement 12.4). This is the pure split the PULSE tab renders from.
export function partitionAlertsByStatus(alerts: Alert[]): {
  live: Alert[];
  resolved: Alert[];
} {
  return {
    live: alerts.filter(isLiveAlert),
    resolved: alerts.filter((alert) => alert.status === 'RESOLVED'),
  };
}

// Apply a tier filter to the live feed. 'ALL' yields every live alert; a specific
// tier yields exactly the live alerts of that tier (Property 23 / Requirement
// 15.5). Pure so the PULSE tab and its tests share one implementation.
export function filterAlertsByTier(liveAlerts: Alert[], tier: TierSelection): Alert[] {
  return tier === 'ALL' ? liveAlerts : liveAlerts.filter((alert) => alert.tier === tier);
}

// Whether an alert has an associated triage brief (drives the agent-ready badge,
// Requirement 10.5).
export function hasTriageBrief(alert: Alert): boolean {
  return Boolean(alert.triageBrief && alert.triageBrief.summary);
}


// Derive up-to-two-letter initials for a VIP avatar (Task 21.2). Prefers an
// explicit initials value from the facade, else builds them from the guest name,
// falling back to a neutral marker when neither is present.
export function initialsFor(name: string | undefined, explicit?: string): string {
  if (explicit && explicit.trim()) return explicit.trim().slice(0, 2).toUpperCase();
  if (!name || !name.trim()) return '--';
  const parts = name.trim().split(/\s+/);
  const first = parts[0]?.charAt(0) ?? '';
  const last = parts.length > 1 ? parts[parts.length - 1].charAt(0) : '';
  return (first + last).toUpperCase() || '--';
}
