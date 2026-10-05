// Tests for the manual "Take a tour" replay button on the StayOS shell launcher.
//
// The onboarding coachmark normally shows only on a GM's first login (a per-user
// seen-flag in localStorage). The "Take a tour" button lets a signed-in GM
// re-run it any time WITHOUT clearing that seen-flag. These tests pin that
// contract: the button appears only when signed in, replays the tour from step
// 1, and finishing/skipping the replay hides it again while leaving the
// first-login persistence untouched.

import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react';

import FeatureGrid from '@/components/FeatureGrid';

const EMAIL = 'gm@example.com';
// Mirrors useOnboarding's key scheme so we can pre-seed the "already seen" flag
// and isolate the button's replay behavior from the first-login auto-show.
const SEEN_KEY = `stayos.onboarding.v1.${EMAIL}`;

afterEach(() => cleanup());
beforeEach(() => window.localStorage.clear());

describe('FeatureGrid "Take a tour" replay button', () => {
  it('is not rendered when signed out', () => {
    render(<FeatureGrid onLogout={() => {}} />);
    expect(screen.queryByRole('button', { name: /take a tour/i })).toBeNull();
  });

  it('is rendered when signed in', () => {
    // Pre-seed the seen-flag so the first-login tour does not auto-open.
    window.localStorage.setItem(SEEN_KEY, '1');
    render(<FeatureGrid email={EMAIL} onLogout={() => {}} />);
    expect(
      screen.getByRole('button', { name: /take a tour/i }),
    ).toBeInTheDocument();
  });

  it('replays the tour from step 1 and hides it again on finish, without re-arming first-login', () => {
    // GM has already seen the tour: the button is the only way to re-open it.
    window.localStorage.setItem(SEEN_KEY, '1');
    render(<FeatureGrid email={EMAIL} onLogout={() => {}} />);

    // Tour is not visible initially (seen-flag set).
    expect(screen.queryByRole('dialog')).toBeNull();

    // Click "Take a tour" -> the coachmark opens at step 1.
    fireEvent.click(screen.getByRole('button', { name: /take a tour/i }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/step 1 of 2/i)).toBeInTheDocument();

    // The button is disabled while the tour is open (no redundant re-trigger).
    expect(screen.getByRole('button', { name: /take a tour/i })).toBeDisabled();

    // Advance step 1 -> step 2 by clicking the bubble.
    fireEvent.click(dialog);
    expect(within(screen.getByRole('dialog')).getByText(/step 2 of 2/i)).toBeInTheDocument();

    // Finish (last step) -> tour hides and the button is enabled again.
    fireEvent.click(screen.getByRole('dialog'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('button', { name: /take a tour/i })).toBeEnabled();

    // The first-login seen-flag is still set (replay never cleared it), and a
    // fresh replay starts at step 1 again.
    expect(window.localStorage.getItem(SEEN_KEY)).toBe('1');
    fireEvent.click(screen.getByRole('button', { name: /take a tour/i }));
    expect(within(screen.getByRole('dialog')).getByText(/step 1 of 2/i)).toBeInTheDocument();
  });

  it('Skip closes the replayed tour and keeps the seen-flag', () => {
    window.localStorage.setItem(SEEN_KEY, '1');
    render(<FeatureGrid email={EMAIL} onLogout={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: /take a tour/i }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /skip/i }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(window.localStorage.getItem(SEEN_KEY)).toBe('1');
  });
});
