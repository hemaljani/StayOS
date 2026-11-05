// Exercise the deployed worker script so cached content cannot bypass auth.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { expect, it, vi } from 'vitest';

function worker() {
  const handlers: Record<string, (event: any) => void> = {};
  const fetch = vi.fn().mockResolvedValue({ ok: true });
  const caches = {
    keys: vi.fn().mockResolvedValue([
      'stayos-api-v1', 'stayos-audio-v2', 'stayos-static-v2', 'pulse-static-v1',
    ]),
    delete: vi.fn().mockResolvedValue(true),
    match: vi.fn(), open: vi.fn(),
  };
  const self = {
    addEventListener: (name: string, handler: (event: any) => void) => { handlers[name] = handler; },
    skipWaiting: vi.fn(), clients: { claim: vi.fn() },
  };
  runInNewContext(readFileSync(resolve('public/sw.js'), 'utf8'), {
    self, caches, fetch, URL, Response,
  });
  return { handlers, fetch, caches };
}

it.each([
  'https://api.example/v1/briefs/ALOHA-TYO-001',
  'https://d123.cloudfront.net/briefs/2026/11/03/ALOHA-TYO-001/morning-brief.mp3?Expires=1',
])('always authorizes private content on the network', async (url) => {
  const app = worker();
  let response: Promise<unknown> | undefined;
  const request = { url, method: 'GET', mode: 'cors', destination: '' };
  app.handlers.fetch({ request, respondWith: (value: Promise<unknown>) => { response = value; } });
  await response;
  expect(app.fetch).toHaveBeenCalledWith(request, { cache: 'no-store' });
  expect(app.caches.match).not.toHaveBeenCalled();
  expect(app.caches.open).not.toHaveBeenCalled();
});

it('purges legacy private caches and preserves other modules', async () => {
  const app = worker();
  let done: Promise<unknown> | undefined;
  app.handlers.activate({ waitUntil: (value: Promise<unknown>) => { done = value; } });
  await done;
  expect(app.caches.delete.mock.calls.map(([name]) => name).sort()).toEqual([
    'stayos-api-v1', 'stayos-audio-v2', 'stayos-static-v2',
  ]);
});
