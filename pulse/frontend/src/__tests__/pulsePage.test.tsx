// UI tests for Task 21.7 - PULSE tab empty-state and live-feed updates.
//
// Renders the real PULSE page (src/app/page.tsx) with a mocked useRealtimeAlerts
// so the feed content is controlled. Covers:
//   - Empty-state (Requirement 15.6): selecting a tier with no matching alerts
//     shows the empty-state message while retaining the selected filter.
//   - Live-feed update (Requirement 15.4): when the feed transitions an alert to
//     RESOLVED (as a realtime event would), the card leaves the live feed and
//     appears under resolved history without a reload.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { makeAlert } from './alertFixtures';
import type { Alert } from '@/lib/types';

// Mutable holder the mocked hook reads from, so tests can set the feed per case.
const hookState = vi.hoisted(() => ({
  current: {
    alerts: [] as Alert[],
    loading: false,
    error: null as string | null,
    refetch: () => {},
    assignedIds: new Set<string>(),
    connected: true,
  },
}));

vi.mock('@/hooks/useRealtimeAlerts', () => ({
  useRealtimeAlerts: () => hookState.current,
}));

import PulsePage from '@/app/page';

beforeEach(() => {
  hookState.current = {
    alerts: [],
    loading: false,
    error: null,
    refetch: vi.fn(),
    assignedIds: new Set<string>(),
    connected: true,
  };
});

describe('PULSE tab empty-state (Requirement 15.6)', () => {
  it('shows the empty-state message and retains the filter when no alert matches the tier', () => {
    // One CRITICAL alert in the feed; selecting Info yields no matches.
    hookState.current.alerts = [
      makeAlert({ tier: 'CRITICAL', status: 'UNACKNOWLEDGED', title: 'Critical situation' }),
    ];

    render(<PulsePage />);

    // The critical alert renders in the live feed by default (ALL).
    expect(screen.getByText('Critical situation')).toBeInTheDocument();

    // Select the Info tier - no info alerts exist.
    const infoButton = screen.getByRole('button', { name: /Info/ });
    fireEvent.click(infoButton);

    // Empty-state message for the selected tier is shown...
    expect(screen.getByText('No matching info alerts.')).toBeInTheDocument();
    // ...the non-matching critical alert is hidden from the filtered feed...
    expect(screen.queryByText('Critical situation')).not.toBeInTheDocument();
    // ...and the Info filter selection is retained (pressed).
    expect(infoButton).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('PULSE live feed updates (Requirement 15.4)', () => {
  it('moves a card from the live feed to resolved history when it resolves, without reload', () => {
    const liveAlert = makeAlert({
      alertId: 'live-1',
      tier: 'CRITICAL',
      status: 'UNACKNOWLEDGED',
      title: 'Walk risk detected',
    });
    hookState.current.alerts = [liveAlert];

    const { rerender } = render(<PulsePage />);

    // Initially the card is in the live feed and there is no resolved section.
    expect(screen.getByText('Walk risk detected')).toBeInTheDocument();
    expect(screen.queryByText('No active alerts. All clear.')).not.toBeInTheDocument();

    // Simulate the ALERT_RESOLVED realtime update flipping the same alert.
    hookState.current.alerts = [{ ...liveAlert, status: 'RESOLVED', resolvedBy: 'gm-1' }];
    rerender(<PulsePage />);

    // The live feed is now empty (its only alert resolved)...
    expect(screen.getByText('No active alerts. All clear.')).toBeInTheDocument();
    // ...and the alert still renders (now within the resolved history section).
    expect(screen.getByText('Walk risk detected')).toBeInTheDocument();
    expect(screen.getByText('Resolved by gm-1')).toBeInTheDocument();
  });
});

describe('PULSE auto-refresh (30s polling)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('calls refetch every 30s and the manual button still refetches on click', () => {
    vi.useFakeTimers();
    const refetch = vi.fn();
    hookState.current.refetch = refetch;
    hookState.current.alerts = [
      makeAlert({ tier: 'CRITICAL', status: 'UNACKNOWLEDGED', title: 'Some alert' }),
    ];

    render(<PulsePage />);

    // No auto-refresh has fired yet before the interval elapses.
    expect(refetch).not.toHaveBeenCalled();

    // After 30s, one auto-refresh fires; after another 30s, a second.
    vi.advanceTimersByTime(30_000);
    expect(refetch).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(30_000);
    expect(refetch).toHaveBeenCalledTimes(2);

    // The manual Refresh button remains and still triggers a refetch on click.
    const refreshButton = screen.getByRole('button', { name: /refresh alerts/i });
    fireEvent.click(refreshButton);
    expect(refetch).toHaveBeenCalledTimes(3);
  });
});
