'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

interface AudioState {
  isPlaying: boolean;
  currentTime: number;
  duration: number;
  progress: number;
  error: string | null;
}

/** Preserve playback controls while renewing expiring audio capabilities. */
export function useAudio(
  audioUrl?: string,
  expiresAt?: string,
  renew?: () => Promise<{ audioUrl: string; audioUrlExpiresAt?: string }>,
) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const expirationRef = useRef(expiresAt);
  const pendingRef = useRef(false);
  const generationRef = useRef(0);
  const [state, setState] = useState<AudioState>({
    isPlaying: false,
    currentTime: 0,
    duration: 0,
    progress: 0,
    error: null,
  });

  useEffect(() => {
    if (!audioUrl) return;
    expirationRef.current = expiresAt;
    const generation = ++generationRef.current;
    const audio = new Audio(audioUrl);
    audioRef.current = audio;

    audio.addEventListener('loadedmetadata', () => {
      setState((s) => ({ ...s, duration: audio.duration }));
    });
    audio.addEventListener('timeupdate', () => {
      setState((s) => ({
        ...s,
        currentTime: audio.currentTime,
        progress: audio.duration ? (audio.currentTime / audio.duration) * 100 : 0,
      }));
    });
    audio.addEventListener('ended', () => {
      setState((s) => ({ ...s, isPlaying: false, progress: 100 }));
    });
    audio.addEventListener('error', () => {
      setState((s) => ({ ...s, error: 'Audio playback failed', isPlaying: false }));
    });

    return () => {
      if (generationRef.current === generation) generationRef.current += 1;
      audio.pause();
      audio.src = '';
      if (audioRef.current === audio) audioRef.current = null;
    };
  }, [audioUrl, expiresAt]);

  const toggle = useCallback(async () => {
    const audio = audioRef.current;
    if (!audio || pendingRef.current) return;

    if (state.isPlaying) {
      audio.pause();
      setState((s) => ({ ...s, isPlaying: false }));
    } else {
      pendingRef.current = true;
      const generation = generationRef.current;
      try {
        const expiry = expirationRef.current
          ? Date.parse(expirationRef.current) : Number.POSITIVE_INFINITY;
        if (expiry <= Date.now() + 60_000) {
          if (!renew) throw new Error('Audio authorization expired');
          const fresh = await renew();
          if (generation !== generationRef.current) return;
          const position = audio.currentTime;
          audio.src = fresh.audioUrl;
          expirationRef.current = fresh.audioUrlExpiresAt;
          // Restore the position once the renewed source's metadata is ready.
          audio.addEventListener('loadedmetadata', () => {
            audio.currentTime = position;
          }, { once: true });
        }
        await audio.play();
        if (generation === generationRef.current) {
          setState((s) => ({ ...s, isPlaying: true, error: null }));
        }
      } catch {
        setState((s) => ({ ...s, error: 'Playback failed' }));
      } finally {
        pendingRef.current = false;
      }
    }
  }, [state.isPlaying, renew]);

  return { ...state, toggle };
}
