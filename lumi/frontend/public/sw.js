// LUMI Service Worker - handles caching, offline support, and push notifications.
// LUMI is served under /lumi on the shared StayOS origin, so all app paths are
// prefixed with /lumi. This worker is registered with scope /lumi/.

const STATIC_CACHE = 'stayos-static-v2';
const API_CACHE = 'stayos-api-v1';
const AUDIO_CACHE = 'stayos-audio-v2';
const ALL_CACHES = [STATIC_CACHE, API_CACHE, AUDIO_CACHE];

// Static assets to precache on install (under the /lumi base path). NOTE: we do
// NOT precache the HTML document ('/lumi/') - HTML is always fetched network-first
// (see fetch handler) so a new deploy / auth change is picked up immediately and
// the service worker can never pin a stale app shell. Only immutable assets here.
const PRECACHE_ASSETS = [
  '/lumi/icons/icon-192.png',
  '/lumi/icons/icon-512.png',
  '/lumi/manifest.json',
];

// Install event: precache essential static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => {
      return cache.addAll(PRECACHE_ASSETS);
    })
  );
  // Activate immediately without waiting for existing clients to close
  self.skipWaiting();
});

// Activate event: clean up old versioned caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => !ALL_CACHES.includes(name))
          .map((name) => caches.delete(name))
      );
    })
  );
  // Take control of all open clients immediately
  self.clients.claim();
});

// Fetch event: apply cache strategies based on request type
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // HTML documents (navigations): always network-first with no cached-HTML
  // fallback, so a new deploy / auth change is served immediately and the SW
  // never pins a stale app shell.
  if (request.mode === 'navigate' || request.destination === 'document') {
    event.respondWith(fetch(request));
    return;
  }

  // API responses: network-first with cache fallback
  if (url.pathname.includes('/v1/briefs/')) {
    event.respondWith(networkFirstWithCache(request, API_CACHE));
    return;
  }

  // Audio files: network-first (briefs update daily, must serve latest)
  if (url.pathname.endsWith('.mp3')) {
    event.respondWith(networkFirstWithCache(request, AUDIO_CACHE));
    return;
  }

  // Static assets (JS, CSS, images): cache-first
  if (isStaticAsset(url.pathname)) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  // All other requests: network with no special caching
  event.respondWith(fetch(request));
});

// Push event: show notification when brief is ready
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || 'LUMI Morning Brief Ready';
  const options = {
    body: data.body || 'Your daily intelligence brief is ready.',
    icon: '/lumi/icons/icon-192.png',
    badge: '/lumi/icons/icon-192.png',
    tag: 'stayos-brief-ready',
    renotify: true,
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// NotificationClick: open or focus the PWA
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      // If the PWA is already open, focus it
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          return client.focus();
        }
      }
      // Otherwise, open a new window (LUMI is served under /lumi)
      return self.clients.openWindow('/lumi/');
    })
  );
});

// Cache-first strategy: return cached response, fall back to network
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
    // Return a basic offline response for non-critical assets
    return new Response('', { status: 503, statusText: 'Offline' });
  }
}

// Network-first strategy: try network, fall back to cache
async function networkFirstWithCache(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) {
      return cached;
    }
    return new Response(
      JSON.stringify({ error: { code: 'OFFLINE', message: 'No cached data available' } }),
      { status: 503, headers: { 'Content-Type': 'application/json' } }
    );
  }
}

// Determine if a request is for a static asset
function isStaticAsset(pathname) {
  return /\.(js|css|png|jpg|jpeg|gif|svg|webp|woff2?|ttf|ico)$/.test(pathname);
}
