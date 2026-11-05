// Test the real feed hook, REST hook and protocol client together with network seams.
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mergeEvent, RECONCILE_INTERVAL_MS, useRealtimeAlerts } from '@/hooks/useRealtimeAlerts';
import type { Alert, AlertsListResponse } from '@/lib/types';

const network = vi.hoisted(() => ({
  alerts: [] as Alert[],
  nextRead: null as Promise<AlertsListResponse> | null,
}));
vi.mock('@/lib/auth', () => ({
  isAuthenticated: () => true,
  getCurrentUser: () => ({ propertyId: 'P1', gmAlias: 'gm1' }),
  getIdToken: () => 'test-id-token',
}));
vi.mock('@/lib/api', () => ({
  authFetch: async (path: string) => {
    if (path === '/config/realtime') {
      return { httpEndpoint: 'https://example.invalid', wssEndpoint: 'wss://example.invalid', namespace: 'pulse' };
    }
    if (network.nextRead) {
      const read = network.nextRead;
      network.nextRead = null;
      return read;
    }
    return { alerts: structuredClone(network.alerts) };
  },
}));

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror = null;
  constructor() { Socket.instances.push(this); }
  send(message: string) { this.sent.push(JSON.parse(message)); }
  close() { this.readyState = 3; this.onclose?.(); }
  receive(message: unknown) { this.onmessage?.({ data: JSON.stringify(message) } as MessageEvent); }
  subscribe() {
    this.receive({ type: 'connection_ack' });
    this.receive({ type: 'subscribe_success', id: this.sent[0].id });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', Socket);
  Socket.instances = [];
  network.nextRead = null;
  network.alerts = mergeEvent([], {
    eventType: 'ALERT_CREATED', alertId: 'initial', propertyId: 'P1',
    status: 'UNACKNOWLEDGED', lastStatusChangeAt: '2026-11-03T12:00:00Z',
  });
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it('recovers a missed create and resolution while the socket remains connected', async () => {
  const { result, unmount } = renderHook(() => useRealtimeAlerts());
  await act(async () => {});
  await act(async () => Socket.instances[0].subscribe());
  expect(result.current.connected).toBe(true);
  network.alerts = mergeEvent(network.alerts, {
    eventType: 'ALERT_CREATED', alertId: 'missed', propertyId: 'P1',
    status: 'UNACKNOWLEDGED',
  });
  await act(async () => { vi.advanceTimersByTime(RECONCILE_INTERVAL_MS); });
  expect(result.current.alerts.map((alert) => alert.alertId)).toContain('missed');
  expect(result.current.loading).toBe(false);
  expect(result.current.connected).toBe(true);
  network.alerts = network.alerts.map((alert) => ({ ...alert, status: 'RESOLVED' as const }));
  await act(async () => { vi.advanceTimersByTime(RECONCILE_INTERVAL_MS); });
  expect(result.current.alerts.every((alert) => alert.status === 'RESOLVED')).toBe(true);
  expect(result.current.alerts).toHaveLength(2);
  unmount();
});

it('keeps a live event arriving during an older REST snapshot', async () => {
  const { result, unmount } = renderHook(() => useRealtimeAlerts());
  await act(async () => {});
  const socket = Socket.instances[0];
  await act(async () => socket.subscribe());
  let complete!: (snapshot: AlertsListResponse) => void;
  network.nextRead = new Promise((resolve) => { complete = resolve; });
  act(() => vi.advanceTimersByTime(RECONCILE_INTERVAL_MS));
  act(() => socket.receive({
    type: 'data', id: socket.sent[0].id,
    event: [JSON.stringify({ eventType: 'ALERT_CREATED', alertId: 'during-read', propertyId: 'P1' })],
  }));
  await act(async () => complete({ alerts: network.alerts, count: network.alerts.length }));
  expect(result.current.alerts.map((alert) => alert.alertId)).toContain('during-read');
  unmount();
});

it('reconciles on subscription recovery after a connection gap', async () => {
  const { result, unmount } = renderHook(() => useRealtimeAlerts());
  await act(async () => {});
  await act(async () => Socket.instances[0].subscribe());
  act(() => Socket.instances[0].close());
  expect(result.current.connected).toBe(false);
  network.alerts = mergeEvent(network.alerts, {
    eventType: 'ALERT_CREATED', alertId: 'during-gap', propertyId: 'P1',
  });
  act(() => vi.advanceTimersByTime(1500));
  expect(Socket.instances).toHaveLength(2);
  await act(async () => Socket.instances[1].subscribe());
  expect(result.current.connected).toBe(true);
  expect(result.current.alerts.map((alert) => alert.alertId)).toContain('during-gap');
  unmount();
});
