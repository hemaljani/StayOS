// Exercise the published worker's fetch handler without a browser extension.
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { describe, expect, it, vi } from 'vitest';

function worker() {
  const listeners = new Map<string, (event: unknown) => void>();
  const put = vi.fn(async () => undefined);
  const response = { ok: true, clone: () => ({}) };
  const fetch = vi.fn(async () => response);
  const caches = {
    match: vi.fn(async () => undefined),
    open: vi.fn(async () => ({ put })),
  };
  runInNewContext(readFileSync('public/sw.js', 'utf8'), {
    self: { addEventListener: (type: string, handler: (event: unknown) => void) =>
      listeners.set(type, handler) },
    URL,
    fetch,
    caches,
  });
  return { handle: listeners.get('fetch')!, fetch, put, response };
}

describe('service worker fetch boundaries', () => {
  it.each([
    ['chrome-extension://test/content.js', 'GET'],
    ['data:text/javascript,test', 'GET'],
    ['https://example.com/v1/briefs/current', 'POST'],
  ])('does not intercept unsupported cache request %s %s', (url, method) => {
    const { handle, fetch, put } = worker();
    const respondWith = vi.fn();
    handle({ request: { url, method }, respondWith });
    expect(respondWith).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
  });

  it('continues caching an HTTPS static asset', async () => {
    const { handle, fetch, put, response } = worker();
    const respondWith = vi.fn();
    const request = { url: 'https://example.com/lumi/app.js', method: 'GET' };
    handle({ request, respondWith });
    expect(await respondWith.mock.calls[0][0]).toBe(response);
    expect(fetch).toHaveBeenCalledWith(request);
    expect(put).toHaveBeenCalledOnce();
  });
});
