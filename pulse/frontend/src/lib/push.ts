// push - Web Push (VAPID) subscription flow for the PULSE PWA (Task 21.3).
//
// Registers the browser's Web Push subscription so the delivery layer can wake a
// closed/backgrounded app for CRITICAL/WARNING alerts (Requirement 13 background
// wake). Flow: confirm support -> gate on notification permission -> fetch the
// VAPID public key from GET /config/vapid-public-key -> pushManager.subscribe ->
// register the subscription with POST /push-subscriptions (body { endpoint,
// p256dh, auth }, matching backend subscriptions.py).
//
// Every step is best-effort and non-throwing: an unsupported browser, a denied
// permission, or a missing VAPID key returns a status the caller can log without
// interrupting the app. The service worker (public/sw.js) handles the resulting
// push and notificationclick events.

import { authFetch } from './api';
import type { VapidPublicKeyResponse } from './types';

// Outcome of a subscribe attempt (for logging / optional UI).
export type PushSubscribeResult =
  | 'subscribed'
  | 'already-subscribed'
  | 'unsupported'
  | 'permission-denied'
  | 'no-vapid-key'
  | 'error';

// Convert a Base64URL VAPID public key to the Uint8Array applicationServerKey
// expects.
function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    output[i] = raw.charCodeAt(i);
  }
  return output;
}

// Whether this browser supports service workers + the Push API.
export function isPushSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  );
}

// Register the browser's push subscription with the backend. Requests
// notification permission when it has not yet been decided; does nothing beyond
// reporting a status when unsupported or denied.
export async function subscribeToPush(): Promise<PushSubscribeResult> {
  if (!isPushSupported()) return 'unsupported';

  // Gate on notification permission (Requirement 13). Request it once when the
  // user has not decided; respect an explicit denial.
  let permission = Notification.permission;
  if (permission === 'default') {
    try {
      permission = await Notification.requestPermission();
    } catch {
      return 'error';
    }
  }
  if (permission !== 'granted') return 'permission-denied';

  try {
    const registration = await navigator.serviceWorker.ready;

    // Reuse an existing subscription when present; re-register it with the
    // backend so the caller's identity/properties stay current.
    const existing = await registration.pushManager.getSubscription();
    if (existing) {
      await registerSubscription(existing);
      return 'already-subscribed';
    }

    // Fetch the VAPID public key; without it we cannot subscribe.
    const { publicKey } = await authFetch<VapidPublicKeyResponse>('/config/vapid-public-key');
    if (!publicKey) return 'no-vapid-key';

    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    });
    await registerSubscription(subscription);
    return 'subscribed';
  } catch {
    return 'error';
  }
}

// POST the subscription to the backend in the exact shape it expects
// (endpoint + the p256dh/auth encryption keys).
async function registerSubscription(subscription: PushSubscription): Promise<void> {
  const json = subscription.toJSON();
  const keys = json.keys ?? {};
  await authFetch('/push-subscriptions', {
    method: 'POST',
    body: JSON.stringify({
      endpoint: json.endpoint,
      p256dh: keys.p256dh,
      auth: keys.auth,
    }),
  });
}
