'use client';

import { useAudio } from '@/hooks/useAudio';

interface AudioBriefPlayerProps {
  audioUrl?: string;
  durationSeconds?: number;
}

export default function AudioBriefPlayer({ audioUrl, durationSeconds }: AudioBriefPlayerProps) {
  const { isPlaying, progress, duration, currentTime, error, toggle } = useAudio(audioUrl);

  const displayDuration = duration || durationSeconds || 0;
  const formatTime = (seconds: number) => {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  return (
    <div className="bg-surface rounded-xl p-4 mb-4">
      <div className="flex items-center gap-3">
        {/* Play/Pause button */}
        <button
          onClick={toggle}
          disabled={!audioUrl}
          className="w-11 h-11 flex items-center justify-center bg-accent rounded-full text-white disabled:opacity-30"
          aria-label={isPlaying ? 'Pause' : 'Play'}
        >
          {isPlaying ? '⏸' : '▶'}
        </button>

        {/* Waveform + progress */}
        <div className="flex-1">
          <div className="flex items-end gap-[2px] h-6 mb-1">
            {Array.from({ length: 24 }).map((_, i) => {
              const barHeight = 8 + Math.sin(i * 0.8) * 12 + 4;
              const isLit = (i / 24) * 100 <= progress;
              return (
                <div
                  key={i}
                  className={`w-[3px] rounded-full transition-colors ${
                    isLit ? 'bg-accent' : 'bg-gray-700'
                  }`}
                  style={{ height: `${barHeight}px` }}
                />
              );
            })}
          </div>
          <div className="flex justify-between text-xs text-gray-500">
            <span>{formatTime(currentTime)}</span>
            <span>{formatTime(displayDuration)}</span>
          </div>
        </div>
      </div>

      {/* Attribution */}
      <p className="text-[10px] text-gray-600 mt-2">Powered by Amazon Bedrock</p>

      {/* Error state */}
      {error && <p className="text-xs text-danger mt-1">{error}</p>}
    </div>
  );
}
