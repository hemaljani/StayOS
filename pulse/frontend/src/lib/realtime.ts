// realtime - AWS AppSync Events WebSocket client for the PULSE live feed (Task 21.3).
//
// On load the client fetches the realtime endpoint from GET /config/realtime,
// opens a WebSocket to the wss endpoint (subprotocol `aws-appsync-event-ws`),
// authorizes each operation with the caller's Cognito JWT, and subscribes to the
// property broadcast channel(s) and the caller's own per-user escalation channel
// under the `pulse` namespace. Incoming ALERT_CREATED / ALERT_UPDATED /
// ALERT_RESOLVED events drive the feed without polling (design Component 6 /
// Requirement 15.4).
//
// Auth handshake (AWS AppSync Events protocol): the authorization credentials
// are wrapped in a JSON object, Base64URL-encoded, and passed as an extra
// WebSocket subprotocol string `header-<encoded>` alongside `aws-appsync-event-ws`.
// For Cognito the object is { Authorization: <idToken>, host: <http-endpoint host> }.
// The same authorization object is included on every `subscribe` message. See
// https://docs.aws.amazon.com/appsync/latest/eventapi/event-api-websocket-protocol.html
//
// Reconnect uses exponential backoff with jitter; on each (re)subscribe the
// caller reconciles missed state via GET /alerts so no update is lost during a
// gap. When realtime config is missing the client stays disconnected and the UI
// falls back to manual refresh (design Component 6 graceful fallback).

import { authFetch } from './api';
import { getIdToken } from './auth';
import type { RealtimeAlertEvent, RealtimeConfigResponse } from './types';

// WebSocket subprotocol required by AppSync Events.
const EVENTS_SUBPROTOCOL = 'aws-appsync-event-ws';

// Backoff bounds for reconnect (ms).
const BASE_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30000;

// If no keep-alive ("ka") arrives within this multiple of the server-provided
// timeout, treat the connection as dead and reconnect. AppSync sends "ka" every
// ~60s; the server ack carries the true timeout (default 300000ms).
const DEFAULT_CONNECTION_TIMEOUT_MS = 300000;

// The authorization object shape for Cognito user-pool auth.
interface EventsAuth {
  Authorization: string;
  host: string;
}

// Callbacks the hook wires into the client.
export interface RealtimeHandlers {
  // A feed-driving event arrived on a property broadcast channel.
  onBroadcastEvent: (event: RealtimeAlertEvent) => void;
  // An escalation-targeted event arrived on the caller's per-user channel.
  onUnicastEvent: (event: RealtimeAlertEvent) => void;
  // A (re)subscribe succeeded; the caller should reconcile via GET /alerts.
  onResubscribe: () => void;
  // Connection state changed (drives an optional "live" indicator).
  onConnectionChange?: (connected: boolean) => void;
}

// Fetch the realtime endpoint config. Returns null when unset/unavailable so the
// caller can fall back to manual refresh.
export async function fetchRealtimeConfig(): Promise<RealtimeConfigResponse | null> {
  try {
    const config = await authFetch<RealtimeConfigResponse>('/config/realtime');
    if (!config.wssEndpoint || !config.httpEndpoint) return null;
    return config;
  } catch {
    return null;
  }
}

// Base64URL-encode a JSON-serializable object (AppSync Events auth encoding).
function base64UrlEncode(value: unknown): string {
  const json = JSON.stringify(value);
  // btoa handles Latin-1; JWTs and hostnames are ASCII so this is safe here.
  return btoa(json).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// Derive the host header (AppSync HTTP endpoint host) from the config. The auth
// `host` must be the HTTP endpoint host even though the socket targets wss.
function hostFromHttpEndpoint(httpEndpoint: string): string {
  try {
    return new URL(httpEndpoint).host;
  } catch {
    // httpEndpoint may already be a bare host; strip any scheme/path defensively.
    return httpEndpoint.replace(/^https?:\/\//, '').split('/')[0];
  }
}

// A minimal, dependency-free AppSync Events subscriber for the PULSE channels.
// One instance owns one WebSocket and its subscriptions; call start() once and
// stop() on unmount.
export class RealtimeClient {
  private readonly config: RealtimeConfigResponse;
  private readonly propertyId: string;
  private readonly gmAlias: string;
  private readonly handlers: RealtimeHandlers;

  private socket: WebSocket | null = null;
  private stopped = false;
  private backoffAttempt = 0;
  private connectionTimeoutMs = DEFAULT_CONNECTION_TIMEOUT_MS;
  private kaTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  // Map of subscription id -> channel kind so a `data` message can be routed to
  // the correct handler (broadcast feed vs per-user escalation nudge).
  private readonly subscriptions = new Map<string, 'broadcast' | 'unicast'>();

  constructor(
    config: RealtimeConfigResponse,
    propertyId: string,
    gmAlias: string,
    handlers: RealtimeHandlers
  ) {
    this.config = config;
    this.propertyId = propertyId;
    this.gmAlias = gmAlias;
    this.handlers = handlers;
  }

  // Build the current Cognito authorization object, or null when no token.
  private authObject(): EventsAuth | null {
    const token = getIdToken();
    if (!token) return null;
    return { Authorization: token, host: hostFromHttpEndpoint(this.config.httpEndpoint) };
  }

  // The two channels this caller subscribes to under the `pulse` namespace.
  private channels(): { channel: string; kind: 'broadcast' | 'unicast' }[] {
    const namespace = this.config.namespace || 'pulse';
    const list: { channel: string; kind: 'broadcast' | 'unicast' }[] = [
      { channel: `/${namespace}/alerts/${this.propertyId}`, kind: 'broadcast' },
    ];
    if (this.gmAlias) {
      list.push({
        channel: `/${namespace}/alerts/${this.propertyId}/${this.gmAlias}`,
        kind: 'unicast',
      });
    }
    return list;
  }

  // Open the socket and begin the handshake. Safe to call repeatedly (a live
  // socket short-circuits).
  start(): void {
    this.stopped = false;
    this.connect();
  }

  // Permanently stop: close the socket and cancel timers. Call on unmount.
  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.subscriptions.clear();
    if (this.socket) {
      try {
        this.socket.close();
      } catch {
        // Ignore: closing an already-closing socket is not actionable.
      }
      this.socket = null;
    }
  }

  private connect(): void {
    const auth = this.authObject();
    if (!auth) {
      // No credential: cannot open an authorized socket. The UI keeps its manual
      // refresh; a later auth refresh + remount re-attempts realtime.
      return;
    }
    const authProtocol = `header-${base64UrlEncode(auth)}`;
    try {
      this.socket = new WebSocket(this.config.wssEndpoint, [EVENTS_SUBPROTOCOL, authProtocol]);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.socket.onopen = () => {
      // Initiate the session; the server replies with connection_ack.
      this.send({ type: 'connection_init' });
    };
    this.socket.onmessage = (event) => this.handleMessage(event);
    this.socket.onerror = () => {
      // onclose follows; reconnect is scheduled there.
    };
    this.socket.onclose = () => {
      this.handlers.onConnectionChange?.(false);
      this.clearKaTimer();
      if (!this.stopped) this.scheduleReconnect();
    };
  }

  private handleMessage(event: MessageEvent): void {
    let message: Record<string, unknown>;
    try {
      message = JSON.parse(String(event.data));
    } catch {
      return;
    }
    const type = message.type;

    if (type === 'connection_ack') {
      const timeout = Number(message.connectionTimeoutMs);
      this.connectionTimeoutMs = Number.isFinite(timeout) && timeout > 0 ? timeout : DEFAULT_CONNECTION_TIMEOUT_MS;
      this.backoffAttempt = 0;
      this.handlers.onConnectionChange?.(true);
      this.resetKaTimer();
      this.subscribeAll();
      return;
    }
    if (type === 'ka') {
      // Keep-alive: reset the dead-connection watchdog.
      this.resetKaTimer();
      return;
    }
    if (type === 'subscribe_success') {
      // A subscription is active; reconcile any state missed during the gap.
      this.handlers.onResubscribe();
      return;
    }
    if (type === 'data') {
      this.handleData(message);
      return;
    }
    // subscribe_error / broadcast_error: log and let backoff/reconnect handle it.
    if (type === 'subscribe_error' || type === 'broadcast_error') {
      console.warn('[realtime] operation error', message);
    }
  }

  // Parse a `data` message's event payload and route it by subscription id.
  private handleData(message: Record<string, unknown>): void {
    const id = String(message.id ?? '');
    const kind = this.subscriptions.get(id);
    if (!kind) return;

    // The `event` field is a stringified JSON value (AppSync Events). Defend
    // against an array-wrapped form as well.
    const raw = message.event;
    const payloads: unknown[] = Array.isArray(raw) ? raw : [raw];
    for (const payload of payloads) {
      const parsed = this.parseEvent(payload);
      if (!parsed) continue;
      if (kind === 'unicast') this.handlers.onUnicastEvent(parsed);
      else this.handlers.onBroadcastEvent(parsed);
    }
  }

  // Parse a single event payload (string or object) into a RealtimeAlertEvent.
  private parseEvent(payload: unknown): RealtimeAlertEvent | null {
    let obj: unknown = payload;
    if (typeof payload === 'string') {
      try {
        obj = JSON.parse(payload);
      } catch {
        return null;
      }
    }
    if (!obj || typeof obj !== 'object') return null;
    const candidate = obj as Partial<RealtimeAlertEvent>;
    if (!candidate.eventType || !candidate.alertId || !candidate.propertyId) return null;
    return candidate as RealtimeAlertEvent;
  }

  // Send all subscribe messages (called after connection_ack and on reconnect).
  private subscribeAll(): void {
    const auth = this.authObject();
    if (!auth) return;
    this.subscriptions.clear();
    for (const { channel, kind } of this.channels()) {
      const id = `${kind}-${crypto.randomUUID()}`;
      this.subscriptions.set(id, kind);
      this.send({ type: 'subscribe', id, channel, authorization: auth });
    }
  }

  private send(message: Record<string, unknown>): void {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
    }
  }

  // Schedule a reconnect with exponential backoff + jitter.
  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    const exponential = Math.min(BASE_BACKOFF_MS * 2 ** this.backoffAttempt, MAX_BACKOFF_MS);
    const jitter = Math.random() * exponential * 0.3;
    const delay = exponential + jitter;
    this.backoffAttempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.stopped) this.connect();
    }, delay);
  }

  // Watchdog: if no "ka" arrives within the connection timeout, force a reconnect.
  private resetKaTimer(): void {
    this.clearKaTimer();
    this.kaTimer = setTimeout(() => {
      if (this.socket) {
        try {
          this.socket.close();
        } catch {
          // Ignore.
        }
      }
    }, this.connectionTimeoutMs);
  }

  private clearKaTimer(): void {
    if (this.kaTimer) {
      clearTimeout(this.kaTimer);
      this.kaTimer = null;
    }
  }

  private clearTimers(): void {
    this.clearKaTimer();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}
