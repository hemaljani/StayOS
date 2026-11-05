// Verify missed-publish recovery and snapshot races without a live AWS session.
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mergeEvent, reconcileSnapshot, RECONCILE_INTERVAL_MS, useRealtimeAlerts } from '@/hooks/useRealtimeAlerts';
import type { RealtimeAlertEvent } from '@/lib/types';
import type { RealtimeHandlers } from '@/lib/realtime';

const mocks = vi.hoisted(() => ({
  refetch: vi.fn(async () => {}),
  stop: vi.fn(),
  alerts: [],
  handlers: null as RealtimeHandlers | null,
}));
vi.mock('@/hooks/useAlerts', () => ({
  useAlerts: () => ({
    alerts: mocks.alerts, snapshotStartedAt: 1, loading: false, error: null, refetch: mocks.refetch,
  }),
}));
vi.mock('@/lib/auth', () => ({
  getCurrentUser: () => ({ propertyId: 'P1', gmAlias: 'gm1' }),
}));
vi.mock('@/lib/realtime', () => ({
  fetchRealtimeConfig: async () => ({ httpEndpoint: 'https://example', wssEndpoint: 'wss://example' }),
  RealtimeClient: class {
    constructor(_config: unknown, _property: string, _alias: string, handlers: RealtimeHandlers) {
      mocks.handlers = handlers;
    }
    start() {}
    stop = mocks.stop;
  },
}));

const event: RealtimeAlertEvent = {
  eventType: 'ALERT_CREATED', alertId: 'a1', propertyId: 'P1',
  status: 'UNACKNOWLEDGED', lastStatusChangeAt: '2026-11-03T12:00:00Z',
};

describe('connected clients recover missed publishes', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mocks.handlers = null;
  });
  afterEach(() => { vi.useRealTimers(); });

  it('reconciles without refresh while still connected and releases timers/listeners', async () => {
    const { result, unmount } = renderHook(() => useRealtimeAlerts());
    await act(async () => {});
    act(() => mocks.handlers!.onConnectionChange!(true));
    expect(result.current.connected).toBe(true);
    act(() => vi.advanceTimersByTime(RECONCILE_INTERVAL_MS));
    expect(mocks.refetch).toHaveBeenCalledWith({ background: true });
    expect(result.current.connected).toBe(true);
    act(() => window.dispatchEvent(new Event('online')));
    act(() => window.dispatchEvent(new Event('focus')));
    expect(mocks.refetch).toHaveBeenCalledTimes(3);
    unmount();
    act(() => vi.advanceTimersByTime(RECONCILE_INTERVAL_MS * 2));
    window.dispatchEvent(new Event('online'));
    expect(mocks.refetch).toHaveBeenCalledTimes(3);
    expect(mocks.stop).toHaveBeenCalledOnce();
  });

  it('replays a new event arriving during REST reconciliation', () => {
    const previous = mergeEvent([], event);
    const recovered = reconcileSnapshot([], previous, [{ event, receivedAt: 20 }], 10);
    expect(recovered.map((alert) => alert.alertId)).toEqual(['a1']);
    expect(reconcileSnapshot([], previous, [{ event, receivedAt: 5 }], 10)).toEqual([]);
  });

  it('recovers a missed resolution from the authoritative snapshot', () => {
    const previous = mergeEvent([], event);
    const snapshot = [{ ...previous[0], status: 'RESOLVED' as const }];
    expect(reconcileSnapshot(snapshot, previous, [], 30)[0].status).toBe('RESOLVED');
  });

  it('does not replay an older event over newer authoritative state', () => {
    const newer = mergeEvent([], { ...event, status: 'RESOLVED', lastStatusChangeAt: '2026-11-03T12:01:00Z' });
    const result = reconcileSnapshot(newer, newer, [{ event, receivedAt: 20 }], 10);
    expect(result[0].status).toBe('RESOLVED');
  });

  it('marks an unknown resolved alert as resolved even without a status field', () => {
    const resolved = mergeEvent([], { ...event, eventType: 'ALERT_RESOLVED', status: undefined });
    expect(resolved[0].status).toBe('RESOLVED');
  });
});
