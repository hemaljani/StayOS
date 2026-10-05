// useOnboarding - per-user "first login" gate for the StayOS onboarding tour.
//
// The shell shows a two-step coachmark (LUMI then PULSE) the first time a given
// GM signs in on this browser, then never again. There is no server-side
// "first login" flag (the shared Cognito users carry no such attribute), so we
// persist a per-user seen-flag in localStorage keyed by the GM's email. Keying
// by email means each GM sees the tour once on a shared browser, rather than
// only the first person to ever use the device.
//
// SSR-safe: the shell is a static export, so every storage access guards
// `typeof window`. The initial state is always "not active" and only flips on
// after the mount effect resolves, avoiding a hydration flash of the tour.

'use client';

import { useCallback, useEffect, useState } from 'react';

// Namespace matches the shared session keys (`stayos.*`) for consistency. The
// email suffix scopes the flag per GM. Bump the version segment if the tour
// content changes enough to warrant re-showing it.
const ONBOARDING_KEY_PREFIX = 'stayos.onboarding.v1.';

/**
 * Build the per-user localStorage key for a given email.
 */
function onboardingKey(email: string): string {
  return `${ONBOARDING_KEY_PREFIX}${email.toLowerCase()}`;
}

interface UseOnboardingResult {
  // True only when the current GM has not yet seen the tour on this browser.
  showTour: boolean;
  // Mark the tour as seen for this GM and hide it (called on finish or skip).
  dismissTour: () => void;
}

/**
 * Decide whether to show the first-login onboarding tour for the signed-in GM.
 *
 * @param email - The signed-in GM's email, or undefined before it resolves.
 *   The tour never shows without an email (it is the per-user key).
 * @returns Whether to show the tour and a callback to dismiss it permanently.
 */
export function useOnboarding(email?: string): UseOnboardingResult {
  const [showTour, setShowTour] = useState(false);

  // Resolve the seen-flag after mount so SSR/static output never renders the
  // tour (which would flash before the client hydrates and reads storage).
  useEffect(() => {
    if (!email) {
      setShowTour(false);
      return;
    }
    if (typeof window === 'undefined') return;
    const seen = window.localStorage.getItem(onboardingKey(email));
    setShowTour(!seen);
  }, [email]);

  const dismissTour = useCallback(() => {
    setShowTour(false);
    if (email && typeof window !== 'undefined') {
      // Persist so this GM does not see the tour again on this browser.
      window.localStorage.setItem(onboardingKey(email), '1');
    }
  }, [email]);

  return { showTour, dismissTour };
}
