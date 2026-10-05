'use client';

import { useEffect } from 'react';
import { withBase } from '@/lib/constants';

export default function ServiceWorkerRegister() {
  useEffect(() => {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker
        // LUMI is served under /lumi, so the service worker lives at /lumi/sw.js
        // and its scope is /lumi/ (a worker cannot control paths above its own
        // location).
        .register(withBase('/sw.js'), { scope: withBase('/') })
        .then((registration) => {
          // Request notification permission after SW registration
          if ('Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
          }

          // Subscribe to push notifications if permission granted
          if ('Notification' in window && Notification.permission === 'granted') {
            registration.pushManager.getSubscription().then((subscription) => {
              if (!subscription) {
                // Subscribe with VAPID key (log for now - full push endpoint is optional for MVP)
                registration.pushManager
                  .subscribe({ userVisibleOnly: true })
                  .then((sub) => {
                    console.info('[SW] Push subscription created:', sub.endpoint);
                  })
                  .catch(() => {
                    // Push subscription not available (e.g., no VAPID key configured)
                  });
              }
            });
          }
        })
        .catch((err) => {
          console.warn('[SW] Registration failed:', err);
        });
    }
  }, []);

  return null;
}
