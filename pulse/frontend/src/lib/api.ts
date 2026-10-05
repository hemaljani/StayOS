// Authenticated fetch wrapper for the PULSE REST API.
//
// Mirrors LUMI's authFetch: attaches the Cognito idToken as the Authorization
// header, and on a 401 attempts a single token refresh + retry before forcing a
// sign-out and redirect to the StayOS shell ("/") (Requirement 16.5). The PULSE
// API returns errors as { error: { message } } (see backend api/http.py
// error_response), so that shape is parsed for the thrown Error message.

import { API_BASE_URL } from './constants';
import { getIdToken, refreshSession, signOut } from './auth';

// Perform an authenticated JSON request and return the parsed response body.
// The generic T is the expected response body shape.
export async function authFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getIdToken();

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: token } : {}),
      ...options.headers,
    },
  });

  // On 401, try to refresh the session once and retry the original request.
  if (response.status === 401) {
    const refreshed = await refreshSession();
    if (refreshed) {
      const retry = await fetch(`${API_BASE_URL}${path}`, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...options.headers,
          Authorization: refreshed.idToken,
        },
      });
      if (retry.ok) {
        return retry.json();
      }
      // A non-401 failure after refresh is a real error; surface it.
      if (retry.status !== 401) {
        const error = await retry.json().catch(() => ({ error: { message: 'Request failed' } }));
        throw new Error(error.error?.message || `API error: ${retry.status}`);
      }
    }
    // Refresh failed or retry still 401: session is dead. Return to the StayOS
    // shell ("/"), which owns login. "/" is outside PULSE's /pulse basePath, so
    // this is a raw redirect (not withBase).
    signOut();
    if (typeof window !== 'undefined') {
      window.location.href = '/';
    }
    throw new Error('Unauthorized');
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: { message: 'Request failed' } }));
    throw new Error(error.error?.message || `API error: ${response.status}`);
  }

  return response.json();
}
