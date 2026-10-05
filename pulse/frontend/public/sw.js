// PULSE Service Worker - PWA install + offline shell caching + Web Push.
//
// Provides the installable offline shell (precache + cache-first for static
// assets) and the Web Push handlers (Task 21.3): a `push` event shows a
// tier-aware notification built from the payload, and `notificationclick`
// focuses an open PWA window (or opens one) deep-linked to the alert. The client
// subscribe flow (VAPID key fetch, pushManager.subscribe, POST
// /push-subscriptions) lives in src/lib/push.ts; this worker only reacts to
// delivered pushes.

const STATIC_CACHE = 'pulse-static-v2';
const ALL_CACHES = [STATIC_CACHE];

// PULSE is served under /pulse on the shared StayOS CloudFront, so this worker
// (registered at /pulse/sw.js with scope /pulse/) must reference every app URL
// under that base. basePath does not rewrite this file (it is copied verbatim
// from public/), so the prefix is applied explicitly here.
const BASE = '/pulse';

// Static assets to precache on install so the shell loads offline. NOTE: we do
// NOT precache the HTML document (`${BASE}/`) - HTML is always fetched from the
// network (see fetch handler) so a new deploy is picked up immediately and the
// service worker can never pin a stale app shell (which would defeat SSO / login
// changes). Only hashed/immutable static assets are precached.
const PRECACHE_ASSETS = [
  `${BASE}/icons/icon-192.svg`,
  `${BASE}/icons/icon-512.svg`,
  `${BASE}/manifest.json`,
];

// Install: precache the shell and activate immediately.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE_ASSETS))
  );
  self.skipWaiting();
});

// Activate: drop any old versioned caches and take control of open clients.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((name) => !ALL_CACHES.includes(name)).map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

// Fetch: network-first for HTML navigations (so a new deploy / auth change is
// always picked up and the SW can never pin a stale app shell), cache-first for
// hashed static assets, network for everything else. Alert data is always
// fetched from the network so the live feed never shows stale alerts.
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // HTML documents (navigations): never cache-first. Always try the network so
  // the latest deployed shell/auth code is served; there is no cached HTML to
  // fall back to (we no longer precache the document).
  if (request.mode === 'navigate' || request.destination === 'document') {
    event.respondWith(fetch(request));
    return;
  }

  if (isStaticAsset(url.pathname)) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  event.respondWith(fetch(request));
});

// Push: show a tier-aware notification built from the delivered payload. The
// alertId is carried in the notification data so notificationclick can deep-link
// to the specific alert (Task 21.3 / Requirement 13 background wake).
self.addEventListener('push', (event) => {
  const data = event.data ? safeJson(event.data) : {};
  const tier = (data.tier || '').toString().toUpperCase();
  // Prefix the title with a tier marker so CRITICAL pushes read at a glance.
  const tierPrefix = tier === 'CRITICAL' ? 'Critical' : tier === 'WARNING' ? 'Warning' : tier === 'INFO' ? 'Info' : '';
  const title = data.title || (tierPrefix ? `${tierPrefix} alert` : 'PULSE Alert');
  const options = {
    body: data.body || data.detail || 'A new alert requires your attention.',
    icon: `${BASE}/icons/icon-192.svg`,
    badge: `${BASE}/icons/icon-192.svg`,
    tag: data.alertId || 'pulse-alert',
    renotify: true,
    // Elevate CRITICAL notifications so they persist until acted on.
    requireInteraction: tier === 'CRITICAL',
    data: { alertId: data.alertId || null, tier: tier || null },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

// NotificationClick: focus an open PWA window (or open one), deep-linked to the
// alert via the ?alertId= query so the app can surface it (Task 21.3).
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const alertId = event.notification.data && event.notification.data.alertId;
  const targetUrl = alertId ? `${BASE}/?alertId=${encodeURIComponent(alertId)}` : `${BASE}/`;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          // Navigate the existing window to the deep link when supported.
          if ('navigate' in client && alertId) {
            client.navigate(targetUrl).catch(() => {});
          }
          return client.focus();
        }
      }
      return self.clients.openWindow(targetUrl);
    })
  );
});

// Cache-first strategy: serve cached asset, fall back to network and cache it.
async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) {
    return cached;
  }
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return new Response('', { status: 503, statusText: 'Offline' });
  }
}

// Parse a push payload without throwing if it is not JSON.
function safeJson(payload) {
  try {
    return payload.json();
  } catch {
    return {};
  }
}

// Determine whether a request path is a cacheable static asset.
function isStaticAsset(pathname) {
  return /\.(js|css|png|jpg|jpeg|gif|svg|webp|woff2?|ttf|ico)$/.test(pathname);
}
