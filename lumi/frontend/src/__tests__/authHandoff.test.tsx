// Verify real shared auth across the legacy shell and current LUMI bundle.
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({ usePathname: () => '/' }));

function token(extra: Record<string, unknown> = {}) {
  const payload = btoa(JSON.stringify({
    exp: Math.floor(Date.now() / 1000) + 3600,
    ...extra,
  }));
  return `test.${payload}.test`;
}

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe('shell to LUMI authentication handoff', () => {
  it('renders LUMI with an existing sessionStorage login from the deployed shell', async () => {
    window.sessionStorage.setItem('stayos.accessToken', token());
    window.sessionStorage.setItem('stayos.idToken', token({ email: 'legacy@example.com' }));
    window.sessionStorage.setItem('stayos.refreshToken', 'legacy-refresh');

    const { default: AuthGuard } = await import('@/components/AuthGuard');
    render(<AuthGuard><div>LUMI content</div></AuthGuard>);
    expect(await screen.findByText('LUMI content')).toBeInTheDocument();
  });

  it('keeps a legacy session together instead of mixing tokens from two stores', async () => {
    const legacyId = token({ email: 'legacy@example.com' });
    window.sessionStorage.setItem('stayos.accessToken', token());
    window.sessionStorage.setItem('stayos.idToken', legacyId);
    window.sessionStorage.setItem('stayos.refreshToken', 'legacy-refresh');
    window.localStorage.setItem('stayos.idToken', token({ email: 'other@example.com' }));

    const auth = await import('@stayos/auth');
    auth.initAuth({ cognitoClientId: 'test-client', cognitoRegion: 'us-east-1' });
    expect(auth.isAuthenticated()).toBe(true);
    expect(auth.getIdToken()).toBe(legacyId);

    const refreshedId = token({ email: 'legacy@example.com', renewed: true });
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({
        AuthenticationResult: { AccessToken: token(), IdToken: refreshedId },
      }),
    })));
    await auth.refreshSession();
    expect(JSON.parse(vi.mocked(fetch).mock.calls[0][1]!.body as string)
      .AuthParameters.REFRESH_TOKEN).toBe('legacy-refresh');
    expect(window.sessionStorage.getItem('stayos.idToken')).toBe(refreshedId);
    expect(window.sessionStorage.getItem('stayos.refreshToken')).toBe('legacy-refresh');
    expect(auth.getIdToken()).toBe(refreshedId);
  });

  it('uses localStorage for a fresh install without a legacy session', async () => {
    const auth = await import('@stayos/auth');
    auth.setTokens({ accessToken: token(), idToken: token(), refreshToken: 'new-refresh' });
    expect(auth.isAuthenticated()).toBe(true);
    expect(window.localStorage.getItem('stayos.refreshToken')).toBe('new-refresh');
    expect(window.sessionStorage.getItem('stayos.accessToken')).toBeNull();
  });

  it('does not switch identities when the legacy shell clears its login', async () => {
    window.sessionStorage.setItem('stayos.accessToken', token());
    window.sessionStorage.setItem('stayos.idToken', token({ email: 'legacy@example.com' }));
    window.localStorage.setItem('stayos.accessToken', token());
    window.localStorage.setItem('stayos.idToken', token({ email: 'other@example.com' }));
    const auth = await import('@stayos/auth');
    expect(auth.isAuthenticated()).toBe(true);
    window.sessionStorage.clear();
    expect(auth.getIdToken()).toBeNull();
    expect(auth.isAuthenticated()).toBe(false);
  });

  it('clears both stores on sign-out and cannot resurrect another stored session', async () => {
    window.sessionStorage.setItem('stayos.accessToken', token());
    window.sessionStorage.setItem('stayos.idToken', token());
    window.sessionStorage.setItem('stayos.refreshToken', 'legacy-refresh');
    window.localStorage.setItem('stayos.accessToken', token());
    window.localStorage.setItem('stayos.idToken', token());
    window.localStorage.setItem('stayos.refreshToken', 'other-refresh');
    const auth = await import('@stayos/auth');
    expect(auth.isAuthenticated()).toBe(true);
    auth.signOut();
    expect(auth.isAuthenticated()).toBe(false);
    for (const store of [window.localStorage, window.sessionStorage]) {
      for (const key of ['accessToken', 'idToken', 'refreshToken']) {
        expect(store.getItem(`stayos.${key}`)).toBeNull();
      }
    }
  });
});
