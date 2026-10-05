// FeatureGrid - the StayOS shell's authenticated view (the launcher).
//
// Renders the StayOS feature catalog. Active cards are raw anchors to the
// feature app on the shared origin (LUMI at /lumi/, PULSE at /pulse/) - full
// navigations that cross Next basePath boundaries, so plain <a> is correct here
// (next/link would keep the shell's routing context). Because the session lives
// in shared origin storage, the target feature reads it directly with no second
// login (SSO). Inactive cards render as disabled "Coming Soon" tiles. A Logout
// control clears the shared session and returns to the login view.

'use client';

import { useEffect, useRef, useState } from 'react';
import { LogOut, Compass, Settings } from 'lucide-react';
import { FEATURES } from '@/lib/features';
import StayOSLogo from '@/components/StayOSLogo';
import OnboardingTour from '@/components/OnboardingTour';
import { useOnboarding } from '@/hooks/useOnboarding';

/**
 * Derive up-to-two uppercase initials from a display label.
 *
 * Handles both a full name ("Jennifer Smith" -> "JS") and an email
 * ("jsmith@aloha.com" -> "J"), so the identity chip always shows something
 * sensible whether or not a name claim is present.
 */
function initials(label: string): string {
  const source = label.includes('@') ? label.split('@')[0] : label;
  const parts = source.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
  return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
}

interface FeatureGridProps {
  // The signed-in GM's email, used as a fallback identity label and to gate the
  // first-login tour.
  email?: string;
  // The signed-in GM's full display name (Cognito "name" claim), preferred over
  // email in the header identity chip when present.
  name?: string;
  onLogout: () => void;
}

export default function FeatureGrid({ email, name, onLogout }: FeatureGridProps) {
  // First-login coachmark gating (per GM, per browser).
  const { showTour, dismissTour } = useOnboarding(email);

  // Manual replay: the header "Take a tour" button flips this so a GM can
  // re-run the coachmark any time, independent of the first-login gate. It is
  // in-memory only, so replaying never touches the persisted seen-flag.
  const [replayTour, setReplayTour] = useState(false);

  // Account menu: a gear icon in the top-right toggles a small dropdown showing
  // who is signed in plus a Logout action (mirrors LUMI's settings-icon
  // affordance). Closed on outside-click and Escape for accessibility.
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const accountMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!accountMenuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (accountMenuRef.current && !accountMenuRef.current.contains(event.target as Node)) {
        setAccountMenuOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setAccountMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [accountMenuOpen]);

  // The tour is visible on first login OR when manually replayed.
  const tourVisible = (showTour || replayTour) && !!email;

  // Finishing/skipping clears both the first-login gate (persisting the
  // seen-flag) and the in-memory replay flag.
  const finishTour = () => {
    setReplayTour(false);
    dismissTour();
  };

  // Positioning context for the tour bubble, plus per-card refs the tour
  // anchors to. Only the two shipped features (LUMI, PULSE) are highlighted.
  const gridRef = useRef<HTMLDivElement | null>(null);
  const lumiCardRef = useRef<HTMLAnchorElement | null>(null);
  const pulseCardRef = useRef<HTMLAnchorElement | null>(null);
  const cardRefs: Record<string, React.RefObject<HTMLAnchorElement | null>> = {
    lumi: lumiCardRef,
    pulse: pulseCardRef,
  };

  // Two-step tour copy, grounded in the LUMI/PULSE PRFAQ GM benefits.
  const tourSteps = [
    {
      id: 'lumi',
      targetRef: lumiCardRef,
      title: 'Meet LUMI - your AI morning brief',
      body: 'Before your shift, LUMI pulls your KPIs, VIP arrivals, and action items into one screen plus a 60-90s audio brief. Five minutes of clarity so you walk the floor already knowing your day.',
    },
    {
      id: 'pulse',
      targetRef: pulseCardRef,
      title: 'And PULSE - real-time awareness',
      body: 'PULSE keeps you ahead all day: tiered alerts (Critical, Warning, Info) the moment a situation develops - walk risk, a VIP room not ready, an escalating complaint - so you act 45 minutes early, not at the front desk.',
    },
  ];
  return (
    <div className="relative min-h-screen flex flex-col justify-center py-12">
      {/* Manual tour trigger, floated top-left (mirrors Logout top-right).
          Only shown when signed in (the tour anchors to the LUMI/PULSE cards).
          Re-runs the coachmark from step 1 without affecting the first-login
          seen-flag. Disabled while the tour is already visible to avoid a
          redundant re-trigger. */}
      {email && (
        <button
          type="button"
          onClick={() => setReplayTour(true)}
          disabled={tourVisible}
          aria-label="Take a tour"
          className="absolute top-4 left-0 flex items-center gap-1 text-xs text-accent hover:text-accent-deep transition-colors rounded-full px-2 py-1 hover:bg-surface disabled:opacity-40 disabled:pointer-events-none"
        >
          <Compass size={14} aria-hidden />
          <span>Take a tour</span>
        </button>
      )}

      {/* Account menu, top-right. A gear icon (mirroring LUMI's settings
          affordance) opens a dropdown, anchored under the icon, that shows who
          is signed in and a Logout action. */}
      <div ref={accountMenuRef} className="absolute top-4 right-0 z-[60]">
        <button
          type="button"
          onClick={() => setAccountMenuOpen((open) => !open)}
          aria-label="Account menu"
          aria-haspopup="menu"
          aria-expanded={accountMenuOpen}
          className={`relative z-10 flex items-center justify-center rounded-full p-2 text-ink-soft transition-colors hover:bg-surface hover:text-ink ${
            accountMenuOpen ? 'bg-surface text-ink' : ''
          }`}
        >
          <Settings size={18} strokeWidth={1.5} aria-hidden />
        </button>

        {accountMenuOpen && (
          <>
            {/* Transparent full-screen click-catcher: closes on any outside
                click without dimming the page. Escape also closes (in effect). */}
            <button
              type="button"
              aria-hidden
              tabIndex={-1}
              onClick={() => setAccountMenuOpen(false)}
              className="fixed inset-0 z-0 cursor-default"
            />
            {/* Dropdown card, anchored to the gear's top-right corner. */}
            <div
              role="menu"
              aria-label="Account"
              className="absolute right-0 top-full mt-2 z-10 w-64 origin-top-right rounded-2xl border border-line bg-surface shadow-2xl shadow-black/20 overflow-hidden"
            >
            {/* Property brand: which hotel group this session belongs to. */}
            <div className="px-4 py-3 border-b border-line bg-background/40">
              <p className="text-[10px] uppercase tracking-wide text-ink-faint leading-none">
                Property
              </p>
              <p className="mt-1 text-sm font-bold text-ink">Aloha Hotels &amp; Resorts</p>
            </div>

            {/* Signed-in identity: avatar initials + full name (fallback email). */}
            {(name || email) && (
              <div className="flex items-center gap-3 px-4 py-4 border-b border-line">
                <span
                  aria-hidden
                  className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-full bg-accent/15 text-sm font-bold uppercase text-accent-deep"
                >
                  {initials(name || email || '')}
                </span>
                <div className="min-w-0">
                  <p className="text-[10px] uppercase tracking-wide text-ink-faint leading-none">
                    Signed in as
                  </p>
                  <p className="mt-1 text-base font-semibold text-ink truncate">{name || email}</p>
                  {name && email && (
                    <p className="text-xs text-ink-soft truncate">{email}</p>
                  )}
                </div>
              </div>
            )}

            <button
              type="button"
              role="menuitem"
              onClick={onLogout}
              className="flex w-full items-center gap-2 px-4 py-3.5 text-sm text-ink-soft transition-colors hover:bg-background hover:text-ink"
            >
              <LogOut size={15} strokeWidth={1.5} aria-hidden />
              <span>Logout</span>
            </button>
            </div>
          </>
        )}
      </div>

      {/* Centered StayOS logo lockup. The Aloha property brand now lives in the
          account (gear) menu, so it is not duplicated here. */}
      <div className="flex flex-col items-center text-center mb-8">
        <StayOSLogo size={56} wordmarkClassName="text-2xl" />
        <p className="text-sm text-ink-soft mt-2">The Operating System for Hotel Associates</p>
      </div>

      <p className="text-center text-xs text-ink-faint mb-6">
        Powerful AI features designed for hotels of all sizes
      </p>

      {/* Feature grid */}
      <div ref={gridRef} className="relative grid grid-cols-2 gap-3">
        {FEATURES.map((feature) => {
          const className = `relative text-left rounded-xl border p-4 transition-all block ${
            feature.active
              ? 'bg-surface border-accent/40 hover:border-accent active:scale-[0.98] shadow-lg shadow-accent/10'
              : 'bg-surface/60 border-line opacity-80 pointer-events-none'
          }`;

          const inner = (
            <>
              <feature.Icon
                size={24}
                className={feature.active ? 'text-accent mb-2' : 'text-ink-faint mb-2'}
                strokeWidth={1.5}
              />
              <p
                className={`text-sm font-semibold mb-1 ${
                  feature.active ? 'text-ink' : 'text-ink-soft'
                }`}
              >
                {feature.name}
              </p>
              <p className="text-[10px] text-ink-faint line-clamp-2">{feature.description}</p>
              <div className="mt-2">
                {feature.active ? (
                  <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-accent/15 text-accent-deep">
                    Available
                  </span>
                ) : (
                  <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-surface-2 text-ink-faint">
                    Coming Soon
                  </span>
                )}
              </div>
            </>
          );

          // Active features link out to the feature app on the shared origin.
          // A raw <a> is used deliberately (full navigation across basePath).
          if (feature.active && feature.href) {
            return (
              <a
                key={feature.id}
                ref={cardRefs[feature.id]}
                href={feature.href}
                aria-label={feature.name}
                className={className}
              >
                {inner}
              </a>
            );
          }

          return (
            <div key={feature.id} aria-disabled className={className}>
              {inner}
            </div>
          );
        })}

        {/* First-login coachmark. Rendered inside the grid so the bubble's
            absolute position is measured against this positioning context.
            Shown on a GM's first login (showTour) OR when manually replayed via
            the "Take a tour" button (replayTour), and only once an email is
            present. Finishing/skipping routes through finishTour, which persists
            the first-login seen-flag exactly as before. */}
        {tourVisible && (
          <OnboardingTour steps={tourSteps} containerRef={gridRef} onFinish={finishTour} />
        )}
      </div>

      {/* Footer */}
      <div className="text-center mt-8">
        <p className="text-[10px] text-ink-faint">&copy; 2026 Aloha Hotels &amp; Resorts</p>
        <p className="text-[9px] text-ink-faint/70 mt-1">
          Aloha Hotels &amp; Resorts is a fictional brand for demo purposes only.
        </p>
      </div>
    </div>
  );
}
