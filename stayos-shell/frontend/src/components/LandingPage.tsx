// LandingPage - the StayOS shell's public marketing view (Command Deck design).
//
// Shown to unauthenticated visitors at the site root ("/") before they sign in.
// It explains what StayOS is and details its two live features (LUMI and PULSE),
// then hands off to the login form via the onSignIn callback (the parent shell
// swaps this view for the LoginForm in place - no redirect). The platform story
// and feature copy are grounded in the repo README and the shell feature
// catalog so the marketing page stays consistent with what actually ships.
//
// Visual direction ("Command Deck"): an operational, data-forward feel. A live
// status bar sits at the top; each live feature is a console panel that previews
// real GM data (a KPI strip for LUMI, a tiered alert stack for PULSE) so the
// page reads as a working tool rather than a brochure. All values shown are
// static, illustrative demo data (not a live read) - the real numbers appear
// after sign-in inside each feature.

'use client';

import { useState } from 'react';
import {
  Sun,
  Zap,
  Layers,
  Database,
  Smartphone,
  Sparkles,
  BarChart3,
  Users,
  Award,
  BriefcaseBusiness,
  ArrowRight,
  ChevronDown,
  type LucideIcon,
} from 'lucide-react';
import StayOSLogo from '@/components/StayOSLogo';
import FeatureModal from '@/components/FeatureModal';
import ThemeToggle from '@/components/ThemeToggle';

interface LandingPageProps {
  // Swaps the shell to the login form (handled by the parent). Invoked by every
  // "Sign In" call-to-action on the page.
  onSignIn: () => void;
}

// An illustrative KPI shown in the LUMI console panel (static demo values).
interface FeatureKpi {
  value: string;
  label: string;
  // Optional accent for the value (occupancy neutral, ADR up, OOO warning).
  tone?: 'up' | 'warn';
}

// An illustrative alert shown in the PULSE console panel (static demo values).
interface FeatureAlert {
  tier: 'CRITICAL' | 'WARNING' | 'INFO';
  text: string;
  when: string;
}

// A single live feature (LUMI / PULSE) rendered as a detailed console panel.
interface LiveFeature {
  id: string;
  name: string;
  tagline: string;
  Icon: LucideIcon;
  // Accent gradient endpoints (Tailwind color tokens) for the feature mark.
  gradientFrom: string;
  gradientTo: string;
  // Operational status pill text shown top-right of the panel header.
  status: string;
  description: string;
  // Panel-specific data preview: a KPI strip (LUMI) or an alert stack (PULSE).
  kpis?: FeatureKpi[];
  alerts?: FeatureAlert[];
  // Two concrete capability bullets shown under the data preview.
  highlights: string[];
}

// The two shipped features. Copy is derived from the README product vision so
// the landing page never overstates what is live. KPI/alert previews are static
// illustrative demo values (the real data is behind the login).
const LIVE_FEATURES: LiveFeature[] = [
  {
    id: 'lumi',
    name: 'LUMI',
    tagline: 'Start the day informed',
    Icon: Sun,
    gradientFrom: 'from-warning',
    gradientTo: 'to-accent',
    status: 'Ready',
    description:
      'A daily AI-generated brief that pulls the numbers that matter into one screen before the shift starts - so the GM walks the floor already knowing the day.',
    kpis: [
      { value: '87%', label: 'Occupancy' },
      { value: '$248', label: 'ADR', tone: 'up' },
      { value: '9', label: 'OOO rooms', tone: 'warn' },
    ],
    highlights: [
      'A 60-90 second audio brief for a hands-free start to the morning',
      'Voice and chat Q&A over the same live property data',
    ],
  },
  {
    id: 'pulse',
    name: 'PULSE',
    tagline: 'Stay informed all day',
    Icon: Zap,
    gradientFrom: 'from-tier-critical',
    gradientTo: 'to-accent',
    status: 'Monitoring',
    description:
      'Real-time, tiered alerts pushed the moment a situation develops - so the GM acts 45 minutes early instead of finding out at the front desk.',
    alerts: [
      { tier: 'CRITICAL', text: 'Walk risk - +6 rooms', when: 'now' },
      { tier: 'WARNING', text: 'OOO cluster vs group block', when: '2m' },
      { tier: 'INFO', text: 'VIP check-in - David Chen', when: '5m' },
    ],
    highlights: [
      'AI triage explains each alert and recommends the next action',
      'Closed-loop resolution: the human approves, the agent executes',
    ],
  },
];

// Why StayOS as a platform - three pillars from the README ("built once and
// shared"). Kept short; the feature panels carry the detail.
const PLATFORM_PILLARS: { Icon: LucideIcon; title: string; body: string }[] = [
  {
    Icon: Database,
    title: 'One data layer',
    body: 'Reads the property\u2019s existing systems (PMS, revenue, loyalty, facilities) through one shared read-only layer - no new integrations per feature.',
  },
  {
    Icon: Sparkles,
    title: 'One AI pipeline',
    body: 'A shared generate-and-validate pipeline turns that data into proactive intelligence, so every feature speaks with the same grounded voice.',
  },
  {
    Icon: Smartphone,
    title: 'One login, on mobile',
    body: 'Sign in once and every feature is a tap away - delivered mobile-first as a dashboard and an audio brief, before you go looking for it.',
  },
];

// The role-specific features still on the roadmap (the shell\u2019s "Coming Soon"
// catalog). Shown as a compact strip to make the platform story concrete.
const UPCOMING_FEATURES: { name: string; Icon: LucideIcon }[] = [
  { name: 'Revenue Optimizer', Icon: BarChart3 },
  { name: 'Guest Experience', Icon: Users },
  { name: 'Best Practice Coach', Icon: Award },
  { name: 'Portfolio Analyzer', Icon: BriefcaseBusiness },
];

// Tailwind classes for each alert tier's left border + tag chip.
const TIER_STYLES: Record<FeatureAlert['tier'], { border: string; chip: string; label: string }> = {
  CRITICAL: { border: 'border-l-tier-critical', chip: 'bg-tier-critical/15 text-tier-critical', label: 'CRIT' },
  WARNING: { border: 'border-l-tier-warning', chip: 'bg-tier-warning/15 text-tier-warning', label: 'WARN' },
  INFO: { border: 'border-l-tier-info', chip: 'bg-tier-info/15 text-tier-info', label: 'INFO' },
};

export default function LandingPage({ onSignIn }: LandingPageProps) {
  // The live feature whose detail popup is open (null = closed). Tapping a
  // LUMI/PULSE panel opens a centered modal so the page stays uncrowded.
  const [activeFeature, setActiveFeature] = useState<LiveFeature | null>(null);

  return (
    <div className="relative mx-auto flex w-full max-w-[400px] flex-col py-6">
      {/* Decorative sunrise glow behind the hero: a soft amber -> violet dawn
          gradient that nods to LUMI's "start the day" + the StayOS rising-arc
          logo, without a literal skyline. Purely decorative (aria-hidden), sits
          behind content (-z-10), and never intercepts taps (pointer-events-none). */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[420px] animate-[sunrise_7s_ease-in-out_infinite_alternate]"
        style={{
          background:
            'radial-gradient(90% 60% at 50% 8%, rgba(63,124,192,0.14), transparent 60%), radial-gradient(70% 55% at 78% 22%, rgba(125,111,208,0.10), transparent 62%), radial-gradient(60% 50% at 20% 26%, rgba(47,143,136,0.08), transparent 62%)',
        }}
      />
      {/* Keyframes for the slow dawn shimmer (scoped to this page). */}
      <style>{`@keyframes sunrise{0%{opacity:.7;transform:translateY(-6px)}100%{opacity:1;transform:translateY(0)}}`}</style>

      {/* Brand header: StayOS lockup on the left, the light/dark theme toggle on
          the right, with the systems-live status chip beneath. */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <img src="/logo.svg" alt="" width={34} height={34} aria-hidden />
          <span className="text-base font-extrabold tracking-tight">
            <span className="text-ink">Stay</span>
            <span className="text-accent-deep">OS</span>
          </span>
        </div>
        <ThemeToggle />
      </div>

      <div className="mt-6 flex flex-col items-center text-center">
        <img src="/logo.svg" alt="" width={72} height={72} aria-hidden />
        <span className="mt-3 text-2xl font-extrabold tracking-tight">
          <span className="text-ink">Stay</span>
          <span className="text-accent-deep">OS</span>
        </span>
        <span className="mt-3 flex items-center gap-1.5 rounded-lg border border-success/30 bg-success/10 px-2.5 py-1.5 text-[10px] font-semibold tracking-wide text-success">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success shadow-[0_0_8px_theme(colors.success)]" aria-hidden />
          All systems live
        </span>
      </div>

      {/* Hero: a one-line StayOS promise + primary CTA, centered under the brand. */}
      <header className="pt-7 text-center">
        <h1 className="text-3xl font-extrabold leading-tight tracking-tight">
          Your property,{' '}
          <span className="bg-gradient-to-r from-accent to-accent-deep bg-clip-text text-transparent">
            in focus.
          </span>
        </h1>
        <p className="mx-auto mt-4 max-w-[320px] text-sm leading-relaxed text-ink-soft">
          Every system your hotel already runs, turned into proactive intelligence. Optimized for mobile.
        </p>
        <button
          type="button"
          onClick={onSignIn}
          className="mt-6 flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-6 py-3.5 font-semibold text-white shadow-lg shadow-accent/25 transition-transform hover:bg-accent-deep active:scale-[0.98]"
        >
          Sign In <ArrowRight size={17} aria-hidden />
        </button>
      </header>

      {/* Live features: LUMI + PULSE as console panels with a data preview. */}
      <section className="mt-10" aria-labelledby="live-features-heading">
        <h2
          id="live-features-heading"
          className="mb-4 text-[11px] font-bold uppercase tracking-[0.1em] text-ink-faint"
        >
          Live today
        </h2>

        <div className="space-y-4">
          {LIVE_FEATURES.map((feature) => (
            <article
              key={feature.id}
              className="overflow-hidden rounded-xl border border-line bg-surface shadow-sm"
            >
              {/* The whole panel is a button: tapping it opens a popup with the
                  feature's full detail. Keeps the page uncrowded - only the
                  tagline + a data preview show here. */}
              <button
                type="button"
                onClick={() => setActiveFeature(feature)}
                aria-haspopup="dialog"
                className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-background"
              >
                <div
                  className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br ${feature.gradientFrom} ${feature.gradientTo}`}
                >
                  <feature.Icon size={20} className="text-white" strokeWidth={1.75} aria-hidden />
                </div>
                <div>
                  <h3 className="text-base font-extrabold leading-none text-ink">{feature.name}</h3>
                  <p className="mt-1 text-[11px] text-ink-soft">{feature.tagline}</p>
                </div>
                <span className="ml-auto flex items-center gap-1.5 rounded-md border border-success/30 bg-success/10 px-2 py-1 text-[9px] font-semibold text-success">
                  <span className="h-1 w-1 rounded-full bg-success" aria-hidden />
                  {feature.status}
                </span>
                <ChevronDown size={16} className="-rotate-90 text-ink-faint" aria-hidden />
              </button>

              {/* Data preview (the appealing, low-text part): the KPI strip
                  (LUMI) or the tiered alert stack (PULSE). Also opens the popup. */}
              <button
                type="button"
                onClick={() => setActiveFeature(feature)}
                aria-haspopup="dialog"
                aria-label={`See what ${feature.name} does`}
                className="block w-full border-t border-line px-4 py-4 text-left transition-colors hover:bg-background"
              >
                {feature.kpis && (
                  <div className="grid grid-cols-3 gap-2">
                    {feature.kpis.map((kpi) => (
                      <div
                        key={kpi.label}
                        className="rounded-lg border border-line bg-background px-3 py-2.5"
                      >
                        <div
                          className={`text-lg font-extrabold tabular-nums ${
                            kpi.tone === 'up'
                              ? 'text-success'
                              : kpi.tone === 'warn'
                              ? 'text-warning'
                              : 'text-ink'
                          }`}
                        >
                          {kpi.value}
                        </div>
                        <div className="mt-0.5 text-[9px] uppercase tracking-wider text-ink-faint">
                          {kpi.label}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {feature.alerts && (
                  <div className="space-y-1.5">
                    {feature.alerts.map((alert) => {
                      const style = TIER_STYLES[alert.tier];
                      return (
                        <div
                          key={alert.text}
                          className={`flex items-center gap-2.5 rounded-lg border-l-[3px] bg-background px-3 py-2.5 ${style.border}`}
                        >
                          <span
                            className={`rounded px-1.5 py-0.5 text-[8.5px] font-bold tracking-wider ${style.chip}`}
                          >
                            {style.label}
                          </span>
                          <span className="text-[11.5px] text-ink-soft">{alert.text}</span>
                          <span className="ml-auto text-[9px] text-ink-faint">{alert.when}</span>
                        </div>
                      );
                    })}
                  </div>
                )}

                <span className="mt-3 block text-[10px] tracking-wide text-ink-faint">
                  Tap to see what {feature.name} does &rarr;
                </span>
              </button>
            </article>
          ))}
        </div>
      </section>

      {/* Platform story: always expanded. */}
      <section className="mt-10" aria-labelledby="platform-heading">
        <h2
          id="platform-heading"
          className="mb-4 text-[11px] font-bold uppercase tracking-[0.1em] text-ink-faint"
        >
          One platform, not one tool
        </h2>

        <div>
          <p className="mb-4 text-[13px] leading-relaxed text-ink-soft">
            Identity, data, AI, and mobile delivery are built once and shared. New role-specific
            features plug into the same foundation, reading the same data with no new integrations.
          </p>
          <div className="space-y-2.5">
            {PLATFORM_PILLARS.map((pillar) => (
              <div
                key={pillar.title}
                className="flex items-start gap-3 rounded-lg border border-line bg-surface p-3.5 shadow-sm"
              >
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-accent/10">
                  <pillar.Icon size={16} className="text-accent" strokeWidth={1.75} aria-hidden />
                </div>
                <div>
                  <p className="text-[13px] font-bold text-ink">{pillar.title}</p>
                  <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">{pillar.body}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Roadmap: always expanded. */}
      <section className="mt-8" aria-labelledby="roadmap-heading">
        <div className="mb-4 flex items-center gap-2">
          <Layers size={12} className="text-ink-faint" aria-hidden />
          <h2
            id="roadmap-heading"
            className="text-[11px] font-bold uppercase tracking-[0.1em] text-ink-faint"
          >
            More coming
          </h2>
        </div>

        <div>
          <div className="grid grid-cols-2 gap-2">
            {UPCOMING_FEATURES.map((feature) => (
              <div
                key={feature.name}
                className="flex items-center gap-2 rounded-lg border border-dashed border-line bg-surface/60 px-3 py-2.5"
              >
                <feature.Icon size={15} className="flex-shrink-0 text-ink-faint" strokeWidth={1.5} aria-hidden />
                <span className="truncate text-[11.5px] text-ink-soft">{feature.name}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Closing CTA - repeat the sign-in affordance after the story. */}
      <div className="mt-10 flex flex-col items-center">
        <StayOSLogo size={40} wordmarkClassName="text-xl" />
        <button
          type="button"
          onClick={onSignIn}
          className="mt-5 flex items-center justify-center gap-2 rounded-lg bg-accent px-8 py-3 font-semibold text-white shadow-lg shadow-accent/25 transition-transform hover:bg-accent-deep active:scale-[0.98]"
        >
          Sign In <ArrowRight size={17} aria-hidden />
        </button>
      </div>

      {/* Footer - matches the login / grid views. */}
      <footer className="mt-10 text-center">
        <p className="text-[10px] text-ink-faint">&copy; 2026 Aloha Hotels &amp; Resorts</p>
        <p className="mx-auto mt-1 max-w-xs text-[9px] text-ink-faint/70">
          Aloha Hotels &amp; Resorts is a fictional brand for demo purposes only.
        </p>
      </footer>

      {/* Feature detail popup - opened by tapping a LUMI / PULSE panel. */}
      <FeatureModal feature={activeFeature} onClose={() => setActiveFeature(null)} onSignIn={onSignIn} />
    </div>
  );
}
