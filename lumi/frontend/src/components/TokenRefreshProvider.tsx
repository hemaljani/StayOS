'use client';

import { useTokenRefresh } from '@/hooks/useTokenRefresh';

/**
 * Client component that runs the proactive token refresh timer.
 * Included in the root layout so it applies to all authenticated pages.
 */
export default function TokenRefreshProvider() {
  useTokenRefresh();
  return null;
}
