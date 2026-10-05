// GenerateAlertsButton - a demo control that triggers all six PULSE scenarios.
//
// Fires each demo scenario via POST /demo/scenarios/{scenarioId} (run action)
// so the rule engine creates the corresponding alerts. It is RUN-ONLY (never
// reset), so triggering does not clear/auto-resolve previously generated alerts
// - older alerts stay visible in the feed and are cleaned up later by the
// backend 30-minute auto-resolve sweeper.
//
// Guardrail (Requirement: demo button cooldown): after a successful trigger the
// button is disabled for a 2-minute cooldown with a live mm:ss countdown. The
// cooldown deadline is persisted in localStorage so it survives a page refresh
// or remount (a reload cannot bypass the guardrail).

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Zap } from 'lucide-react';
import { authFetch } from '@/lib/api';

// The six demo scenario ids (mirror backend demo_simulator SCENARIOS keys).
const SCENARIO_IDS = [
  'walk-risk',
  'vip-room-not-ready',
  'complaint-escalation',
  'ooo-cluster',
  'premium-cancellation',
  'vip-checkin',
] as const;

// Cooldown window in milliseconds (2 minutes) before the button re-enables.
const COOLDOWN_MS = 120_000;

// localStorage key holding the epoch-ms timestamp until which the button is on
// cooldown. Persisted so a refresh cannot bypass the guardrail.
const COOLDOWN_STORAGE_KEY = 'pulse.generateAlerts.cooldownUntil';

interface GenerateAlertsButtonProps {
  // Called after all scenarios are triggered so the feed can reload.
  onGenerated?: () => void;
}

/**
 * Read the persisted cooldown deadline (epoch ms) from localStorage.
 * Returns 0 when unset, invalid, or already elapsed.
 */
function readCooldownUntil(): number {
  if (typeof window === 'undefined') return 0;
  const raw = window.localStorage.getItem(COOLDOWN_STORAGE_KEY);
  const until = raw ? Number.parseInt(raw, 10) : 0;
  if (!Number.isFinite(until) || until <= Date.now()) return 0;
  return until;
}

/** Format a millisecond remaining span as m:ss (e.g. 119000 -> "1:59"). */
function formatRemaining(ms: number): string {
  const totalSeconds = Math.ceil(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

export default function GenerateAlertsButton({ onGenerated }: GenerateAlertsButtonProps) {
  // Epoch-ms deadline the cooldown ends at; 0 means "not on cooldown".
  const [cooldownUntil, setCooldownUntil] = useState<number>(0);
  // Drives the live countdown re-render; the remaining span in ms.
  const [remainingMs, setRemainingMs] = useState<number>(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Hydrate the cooldown from localStorage on mount so a refresh during an
  // active cooldown keeps the button disabled (guardrail survives reload).
  useEffect(() => {
    const until = readCooldownUntil();
    if (until > 0) {
      setCooldownUntil(until);
      setRemainingMs(until - Date.now());
    }
  }, []);

  // Tick the countdown once per second while on cooldown; clear it when done.
  useEffect(() => {
    if (cooldownUntil <= 0) return;

    const tick = () => {
      const left = cooldownUntil - Date.now();
      if (left <= 0) {
        setCooldownUntil(0);
        setRemainingMs(0);
        if (typeof window !== 'undefined') {
          window.localStorage.removeItem(COOLDOWN_STORAGE_KEY);
        }
        if (tickRef.current) clearInterval(tickRef.current);
        tickRef.current = null;
        return;
      }
      setRemainingMs(left);
    };

    tick();
    tickRef.current = setInterval(tick, 1000);
    return () => {
      if (tickRef.current) clearInterval(tickRef.current);
      tickRef.current = null;
    };
  }, [cooldownUntil]);

  const onCooldown = cooldownUntil > Date.now();
  const disabled = busy || onCooldown;

  const handleClick = useCallback(async () => {
    if (busy || cooldownUntil > Date.now()) return;
    setBusy(true);
    setError(null);
    try {
      // Trigger each scenario sequentially (run-only). Sequential (not parallel)
      // keeps the ordering deterministic and avoids a burst of concurrent
      // stream writes; the set is small (six) so latency is acceptable.
      for (const scenarioId of SCENARIO_IDS) {
        await authFetch(`/demo/scenarios/${scenarioId}`, {
          method: 'POST',
          body: JSON.stringify({}),
        });
      }
      onGenerated?.();
      // Start the 2-minute cooldown and persist the deadline so a refresh
      // cannot re-enable the button early.
      const until = Date.now() + COOLDOWN_MS;
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(COOLDOWN_STORAGE_KEY, String(until));
      }
      setCooldownUntil(until);
      setRemainingMs(COOLDOWN_MS);
    } catch (err: unknown) {
      // A trigger failure must not start the cooldown - the user may retry.
      setError(err instanceof Error ? err.message : 'Failed to generate alerts');
    } finally {
      setBusy(false);
    }
  }, [busy, cooldownUntil, onGenerated]);

  const label = busy
    ? 'Generating...'
    : onCooldown
      ? `Wait ${formatRemaining(remainingMs)}`
      : 'Generate Alerts';

  return (
    <div className="flex flex-col items-end gap-0.5">
      <button
        type="button"
        onClick={handleClick}
        disabled={disabled}
        aria-label="Generate demo alerts"
        title={
          onCooldown
            ? 'Cooldown active - new alerts can be generated when the timer ends'
            : 'Trigger all six demo scenarios'
        }
        className={`flex items-center gap-1 rounded-full border px-3 py-1.5 text-[11px] font-semibold transition-colors ${
          disabled
            ? 'border-gray-700 bg-surface text-gray-500 opacity-60 cursor-not-allowed'
            : 'border-accent bg-accent/10 text-accent hover:bg-accent/20'
        }`}
      >
        <Zap size={12} aria-hidden />
        {label}
      </button>
      {error && (
        <span role="alert" className="text-[10px] text-danger">
          {error}
        </span>
      )}
    </div>
  );
}
