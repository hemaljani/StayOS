// Validate expiring capability renewal without changing the player's controls.
import { act, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useAudio } from '@/hooks/useAudio';

afterEach(() => vi.unstubAllGlobals());

function mockAudio() {
  const listeners: Record<string, () => void> = {};
  const audio = {
    src: '', currentTime: 23, duration: 90,
    play: vi.fn().mockResolvedValue(undefined), pause: vi.fn(),
    addEventListener: vi.fn((name: string, fn: () => void) => { listeners[name] = fn; }),
  };
  vi.stubGlobal('Audio', vi.fn(function (src: string) {
    audio.src = src;
    return audio;
  }));
  return { audio, listeners };
}

it('renews an expired URL and restores the playback position', async () => {
  const { audio, listeners } = mockAudio();
  const renew = vi.fn().mockResolvedValue({
    audioUrl: 'fresh-signed', audioUrlExpiresAt: new Date(Date.now() + 900_000).toISOString(),
  });
  const { result } = renderHook(() => useAudio('old-signed', '2020-01-01T00:00:00Z', renew));
  await act(async () => { await result.current.toggle(); });
  expect(renew).toHaveBeenCalledOnce();
  expect(audio.src).toBe('fresh-signed');
  audio.currentTime = 0;
  act(() => listeners.loadedmetadata());
  expect(audio.currentTime).toBe(23);
  expect(result.current.isPlaying).toBe(true);
});

it('does not play an expired URL if renewal authorization fails', async () => {
  const { audio } = mockAudio();
  const renew = vi.fn().mockRejectedValue(new Error('Forbidden'));
  const { result } = renderHook(() => useAudio('old-signed', '2020-01-01T00:00:00Z', renew));
  await act(async () => { await result.current.toggle(); });
  expect(audio.play).not.toHaveBeenCalled();
  expect(result.current.error).toBe('Playback failed');
  expect(result.current.isPlaying).toBe(false);
});

it('plays a current capability without an unnecessary API request', async () => {
  const { audio } = mockAudio();
  const renew = vi.fn();
  const { result } = renderHook(() => useAudio(
    'valid-signed', new Date(Date.now() + 900_000).toISOString(), renew,
  ));
  await act(async () => { await result.current.toggle(); });
  expect(renew).not.toHaveBeenCalled();
  expect(audio.play).toHaveBeenCalledOnce();
});
