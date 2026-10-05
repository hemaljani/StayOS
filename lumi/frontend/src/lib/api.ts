import { API_BASE_URL } from './constants';
import { signOut, refreshSession, getIdToken } from './auth';

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

  // On 401, attempt token refresh before giving up
  if (response.status === 401) {
    const refreshed = await refreshSession();
    if (refreshed) {
      // Retry the original request with the new token
      const retryResponse = await fetch(`${API_BASE_URL}${path}`, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...options.headers,
          Authorization: refreshed.idToken,
        },
      });

      if (retryResponse.ok) {
        return retryResponse.json();
      }

      // Retry also failed — fall through to sign out
      if (retryResponse.status !== 401) {
        const error = await retryResponse.json().catch(() => ({ error: { message: 'Request failed' } }));
        throw new Error(error.error?.message || `API error: ${retryResponse.status}`);
      }
    }

    // Refresh failed or retry still 401 — force logout to the StayOS shell ("/").
    signOut();
    window.location.href = '/';
    throw new Error('Unauthorized');
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: { message: 'Request failed' } }));
    throw new Error(error.error?.message || `API error: ${response.status}`);
  }

  return response.json();
}
