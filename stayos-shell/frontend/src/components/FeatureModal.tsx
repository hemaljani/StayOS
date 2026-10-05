// FeatureModal - a centered popup dialog with a live feature's full detail.
//
// Opened from the landing page when a GM taps a LUMI / PULSE feature panel. Keeps
// the landing page uncrowded (the panels themselves show only the tagline + a
// data preview) while making the full description + capability highlights one tap
// away. Mirrors the modal conventions already used across StayOS (LUMI's
// AskLumiModal / ChatPanel, PULSE's TriageModal): a React portal above all other
// UI, a dimmed backdrop that dismisses on tap, an explicit close button,
// Escape-to-close, and a focus trap while open. Adds a fade + scale entrance.

'use client';

import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { ArrowRight, Check, X, type LucideIcon } from 'lucide-react';

// A KPI shown in the modal's data preview (LUMI).
export interface FeatureModalKpi {
  value: string;
  label: string;
  tone?: 'up' | 'warn';
}

// An alert shown in the modal's data preview (PULSE).
export interface FeatureModalAlert {
  tier: 'CRITICAL' | 'WARNING' | 'INFO';
  text: string;
  when: string;
}

// The subset of a live feature this modal needs to render its detail.
export interface FeatureModalData {
  id: string;
  name: string;
  tagline: string;
  Icon: LucideIcon;
  gradientFrom: string;
  gradientTo: string;
  description: string;
  highlights: string[];
  kpis?: FeatureModalKpi[];
  alerts?: FeatureModalAlert[];
}

// Tailwind classes for each alert tier's left border + tag chip (mirrors the
// landing page's tier styling so the popup preview matches the panel preview).
const TIER_STYLES: Record<FeatureModalAlert['tier'], { border: string; chip: string; label: string }> = {
  CRITICAL: { border: 'border-l-tier-critical', chip: 'bg-tier-critical/15 text-tier-critical', label: 'CRIT' },
  WARNING: { border: 'border-l-tier-warning', chip: 'bg-tier-warning/15 text-tier-warning', label: 'WARN' },
  INFO: { border: 'border-l-tier-info', chip: 'bg-tier-info/15 text-tier-info', label: 'INFO' },
};

interface FeatureModalProps {
  // The feature to show, or null when the modal is closed.
  feature: FeatureModalData | null;
  onClose: () => void;
  // Invoked by the modal's "Sign in to open X" CTA (parent shows the login form).
  onSignIn: () => void;
}

export default function FeatureModal({ feature, onClose, onSignIn }: FeatureModalProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  const isOpen = feature !== null;

  // Move focus into the dialog when it opens (after the portal renders).
  useEffect(() => {
    if (!isOpen) return;
    const timer = setTimeout(() => closeButtonRef.current?.focus(), 50);
    return () => clearTimeout(timer);
  }, [isOpen]);

  // Escape closes; Tab is trapped within the dialog while open.
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key === 'Tab') {
        const focusable = cardRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (!focusable || focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!feature) return null;

  const modal = (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`${feature.name} details`}
      className="fixed inset-0 z-[70] flex items-center justify-center px-5"
      onClick={onClose}
    >
      {/* Dimmed backdrop (its own layer, fades in). */}
      <div
        className="absolute inset-0 bg-black/70 animate-[featureFade_.2s_ease-out]"
        aria-hidden
      />

      {/* Dialog card - stopPropagation so taps inside don't dismiss. Fades + scales in. */}
      <div
        ref={cardRef}
        onClick={(event) => event.stopPropagation()}
        className="relative w-full max-w-[360px] rounded-xl border border-line bg-surface p-5 shadow-2xl animate-[featurePop_.22s_cubic-bezier(.2,.8,.2,1)]"
      >
        {/* Keyframes scoped to the modal. */}
        <style>{`
          @keyframes featureFade{from{opacity:0}to{opacity:1}}
          @keyframes featurePop{from{opacity:0;transform:translateY(10px) scale(.96)}to{opacity:1;transform:translateY(0) scale(1)}}
        `}</style>

        {/* Close button. */}
        <button
          ref={closeButtonRef}
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute right-3 top-3 flex h-9 w-9 items-center justify-center rounded-full text-ink-faint transition-colors hover:bg-background hover:text-ink focus:outline-none focus:ring-2 focus:ring-accent"
        >
          <X size={18} />
        </button>

        {/* Feature mark + name + tagline. */}
        <div className="flex items-center gap-3 pr-8">
          <div
            className={`flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br ${feature.gradientFrom} ${feature.gradientTo}`}
          >
            <feature.Icon size={24} className="text-white" strokeWidth={1.75} aria-hidden />
          </div>
          <div>
            <h2 className="text-xl font-extrabold leading-none text-ink">{feature.name}</h2>
            <p className="mt-1.5 text-xs text-accent-deep">{feature.tagline}</p>
          </div>
        </div>

        {/* Full description. */}
        <p className="mt-4 text-sm leading-relaxed text-ink-soft">{feature.description}</p>

        {/* Data preview: KPI strip (LUMI) or tiered alert stack (PULSE), so the
            popup is self-contained and shows the feature's live-style data. */}
        {feature.kpis && (
          <div className="mt-4 grid grid-cols-3 gap-2">
            {feature.kpis.map((kpi) => (
              <div key={kpi.label} className="rounded-lg border border-line bg-background px-3 py-2.5">
                <div
                  className={`text-lg font-extrabold tabular-nums ${
                    kpi.tone === 'up' ? 'text-success' : kpi.tone === 'warn' ? 'text-warning' : 'text-ink'
                  }`}
                >
                  {kpi.value}
                </div>
                <div className="mt-0.5 text-[9px] uppercase tracking-wider text-ink-faint">{kpi.label}</div>
              </div>
            ))}
          </div>
        )}

        {feature.alerts && (
          <div className="mt-4 space-y-1.5">
            {feature.alerts.map((alert) => {
              const style = TIER_STYLES[alert.tier];
              return (
                <div
                  key={alert.text}
                  className={`flex items-center gap-2.5 rounded-lg border-l-[3px] bg-background px-3 py-2.5 ${style.border}`}
                >
                  <span className={`rounded px-1.5 py-0.5 text-[8.5px] font-bold tracking-wider ${style.chip}`}>
                    {style.label}
                  </span>
                  <span className="text-[11.5px] text-ink-soft">{alert.text}</span>
                  <span className="ml-auto text-[9px] text-ink-faint">{alert.when}</span>
                </div>
              );
            })}
          </div>
        )}

        {/* Capability highlights. */}
        <ul className="mt-4 space-y-2.5">
          {feature.highlights.map((highlight) => (
            <li key={highlight} className="flex items-start gap-2.5 text-[13px] text-ink-soft">
              <Check size={15} className="mt-0.5 flex-shrink-0 text-accent" aria-hidden />
              <span>{highlight}</span>
            </li>
          ))}
        </ul>

        {/* CTA: sign in to open this feature (closes the popup, then the parent
            shell swaps to the login form). */}
        <button
          type="button"
          onClick={() => {
            onClose();
            onSignIn();
          }}
          className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-6 py-3 font-semibold text-white shadow-lg shadow-accent/25 transition-transform hover:bg-accent-deep active:scale-[0.98]"
        >
          Sign in to open {feature.name} <ArrowRight size={16} aria-hidden />
        </button>
      </div>
    </div>
  );

  // Portal to <body> so the dialog escapes the landing page's stacking context.
  if (typeof document === 'undefined') return null;
  return createPortal(modal, document.body);
}
