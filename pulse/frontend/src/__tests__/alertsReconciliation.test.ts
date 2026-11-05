// Test REST recovery behavior against overlapping and failed requests.
import { act, renderHook } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAlerts } from '@/hooks/useAlerts';

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('@/lib/api', () => ({ authFetch: mocks.fetch }));
vi.mock('@/lib/auth', () => ({
  isAuthenticated: () => true, getCurrentUser: () => ({ propertyId: 'P1' }),
}));
beforeEach(() => vi.clearAllMocks());

it('ignores an older response arriving after a newer background snapshot', async () => {
  let completeOlder!: (value: unknown) => void;
  mocks.fetch.mockImplementationOnce(() => new Promise((resolve) => { completeOlder = resolve; }));
  const { result } = renderHook(() => useAlerts());
  mocks.fetch.mockResolvedValueOnce({ alerts: [{ alertId: 'new' }] });
  await act(() => result.current.refetch({ background: true }));
  await act(async () => completeOlder({ alerts: [{ alertId: 'old' }] }));
  expect(result.current.alerts.map((alert) => alert.alertId)).toEqual(['new']);
});

it('keeps the feed visible when a background reconciliation fails', async () => {
  mocks.fetch.mockResolvedValueOnce({ alerts: [{ alertId: 'existing' }] });
  const { result } = renderHook(() => useAlerts());
  await act(async () => {});
  mocks.fetch.mockRejectedValueOnce(new Error('temporarily unavailable'));
  await act(() => result.current.refetch({ background: true }));
  expect(result.current.alerts.map((alert) => alert.alertId)).toEqual(['existing']);
  expect(result.current.loading).toBe(false);
  expect(result.current.error).toBe('temporarily unavailable');
});
