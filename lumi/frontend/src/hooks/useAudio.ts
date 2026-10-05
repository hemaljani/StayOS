'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

interface AudioState {
  isPlaying: boolean;
  currentTime: number;
  duration: number;
  progress: number;
  error: string | null;
}

export function useAudio(audioUrl?: string) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<AudioState>({
    isPlaying: false,
    currentTime: 0,
    duration: 0,
    progress: 0,
    error: null,
  });

  useEffect(() => {
    if (!audioUrl) return;

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
      audio.pause();
      audio.src = '';
    };
  }, [audioUrl]);

  const toggle = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;

    if (state.isPlaying) {
      audio.pause();
      setState((s) => ({ ...s, isPlaying: false }));
    } else {
      audio.play().catch(() => {
        setState((s) => ({ ...s, error: 'Playback failed' }));
      });
      setState((s) => ({ ...s, isPlaying: true, error: null }));
    }
  }, [state.isPlaying]);

  return { ...state, toggle };
}
