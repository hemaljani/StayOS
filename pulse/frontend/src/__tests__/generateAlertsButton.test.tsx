// UI tests for the GenerateAlertsButton demo control.
//
// Verifies the two guardrail requirements: (1) a click triggers all six demo
// scenarios (run-only POSTs) and then reloads the feed; (2) after a successful
// trigger the button is disabled for a 2-minute cooldown with a live countdown,
// and the cooldown persists across a remount (localStorage-backed) so a refresh
// cannot bypass it.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';

// Mock the auth client so no real network call is made.
const authFetch = vi.fn();
vi.mock('@/lib/api', () => ({
  authFetch: (path: string, options?: RequestInit) => authFetch(path, options),
}));

import GenerateAlertsButton from '@/components/GenerateAlertsButton';

const SCENARIO_IDS = [
  'walk-risk',
  'vip-room-not-ready',
  'complaint-escalation',
  'ooo-cluster',
  'premium-cancellation',
  'vip-checkin',
];

describe('GenerateAlertsButton', () => {
  beforeEach(() => {
    authFetch.mockReset();
    authFetch.mockResolvedValue({});
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('triggers all six scenarios (run-only) then calls onGenerated', async () => {
    const onGenerated = vi.fn();
    render(<GenerateAlertsButton onGenerated={onGenerated} />);

    fireEvent.click(screen.getByRole('button', { name: /generate demo alerts/i }));

    await waitFor(() => expect(authFetch).toHaveBeenCalledTimes(6));

    // Each scenario was POSTed to its run route (no /reset).
    SCENARIO_IDS.forEach((id) => {
      expect(authFetch).toHaveBeenCalledWith(
        `/demo/scenarios/${id}`,
        expect.objectContaining({ method: 'POST' })
      );
    });
    expect(authFetch).not.toHaveBeenCalledWith(
      expect.stringContaining('/reset'),
      expect.anything()
    );
    await waitFor(() => expect(onGenerated).toHaveBeenCalledTimes(1));
  });

  it('disables the button for a 2-minute cooldown with a countdown', async () => {
    vi.useFakeTimers();
    render(<GenerateAlertsButton />);
    const button = screen.getByRole('button', { name: /generate demo alerts/i });

    expect(button).toBeEnabled();

    // Click and let the six awaited POSTs resolve (flush microtasks).
    await act(async () => {
      fireEvent.click(button);
    });

    // Now on cooldown: disabled and showing a mm:ss countdown near 2:00.
    expect(button).toBeDisabled();
    expect(button.textContent).toMatch(/Wait [12]:\d{2}/);

    // Just before 2 minutes: still disabled.
    await act(async () => {
      vi.advanceTimersByTime(119_000);
    });
    expect(button).toBeDisabled();

    // At 2 minutes: re-enabled and back to the default label.
    await act(async () => {
      vi.advanceTimersByTime(1_000);
    });
    expect(button).toBeEnabled();
    expect(button.textContent).toMatch(/Generate Alerts/);
  });

  it('persists the cooldown across a remount (survives refresh)', async () => {
    vi.useFakeTimers();
    const first = render(<GenerateAlertsButton />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /generate demo alerts/i }));
    });
    expect(screen.getByRole('button', { name: /generate demo alerts/i })).toBeDisabled();

    // Simulate a page refresh: unmount and mount a fresh instance. The cooldown
    // deadline lives in localStorage, so the new instance is still disabled.
    first.unmount();
    render(<GenerateAlertsButton />);

    const remounted = screen.getByRole('button', { name: /generate demo alerts/i });
    expect(remounted).toBeDisabled();
    expect(remounted.textContent).toMatch(/Wait [12]:\d{2}/);
  });

  it('does not start the cooldown if a scenario trigger fails', async () => {
    authFetch.mockRejectedValueOnce(new Error('boom'));
    render(<GenerateAlertsButton />);
    const button = screen.getByRole('button', { name: /generate demo alerts/i });

    await act(async () => {
      fireEvent.click(button);
    });

    // The trigger failed before the cooldown started, so the button re-enables
    // and the error is surfaced.
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByRole('alert')).toHaveTextContent('boom');
  });
});
