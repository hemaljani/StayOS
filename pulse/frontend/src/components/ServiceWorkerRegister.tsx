// ServiceWorkerRegister - registers the PULSE service worker and Web Push (Task 21.3).
//
// Registers /sw.js for PWA install + offline shell, then attempts the Web Push
// subscription flow (src/lib/push.ts): it gates on notification permission and
// registers the subscription with the backend so the delivery layer can wake a
// closed/backgrounded app (Requirement 13). Only attempts a subscribe for
// authenticated users (the subscribe call is authorized) and skips silently on
// unsupported browsers or when permission is denied. Renders nothing.

'use client';

import { useEffect } from 'react';
import { isAuthenticated } from '@/lib/auth';
import { subscribeToPush } from '@/lib/push';
import { BASE_PATH } from '@/lib/constants';

export default function ServiceWorkerRegister() {
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;

    // PULSE is served under BASE_PATH (/pulse) on the shared StayOS CloudFront,
    // so the worker is at /pulse/sw.js and its scope is /pulse/ (a worker can
    // only control paths at or below its own scope). basePath does not rewrite
    // this raw URL, so it is prefixed explicitly.
    navigator.serviceWorker
      .register(`${BASE_PATH}/sw.js`, { scope: `${BASE_PATH}/` })
      .then(() => {
        // Attempt the push subscription only for signed-in users; the subscribe
        // call is authorized and scoped to the caller's properties server-side.
        if (isAuthenticated()) {
          subscribeToPush().then((result) => {
            if (result === 'error') {
              console.warn('[Push] Subscription attempt failed');
            }
          });
        }
      })
      .catch((err) => {
        console.warn('[SW] Registration failed:', err);
      });
  }, []);

  return null;
}
