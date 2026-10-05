// TokenRefreshProvider - runs the proactive token-refresh timer app-wide.
//
// Mirrors LUMI: mounted in the root layout so the Cognito session stays fresh on
// every authenticated page. Renders nothing.

'use client';

import { useTokenRefresh } from '@/hooks/useTokenRefresh';

export default function TokenRefreshProvider() {
  useTokenRefresh();
  return null;
}
