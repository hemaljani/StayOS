'use client';

import { useEffect, useState } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
import { authFetch } from '@/lib/api';
import { getCurrentUser, isAuthenticated } from '@/lib/auth';
import AudioBriefPlayer from '@/components/AudioBriefPlayer';
import KpiGrid from '@/components/KpiGrid';
import type { BriefResponse } from '@/lib/types';

interface BriefDetailState {
  brief: BriefResponse | null;
  loading: boolean;
  error: string | null;
}

/**
 * Formats a YYYY-MM-DD string into a human-readable date like "Sunday, August 2, 2026"
 */
function formatFullDate(dateStr: string): string {
  const date = new Date(dateStr + 'T00:00:00');
  return date.toLocaleDateString('en-US', {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
}

export default function BriefDetail() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const briefDate = searchParams.get('date') || '';

  const [state, setState] = useState<BriefDetailState>({
    brief: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    if (!briefDate) {
      setState({ brief: null, loading: false, error: 'No date specified' });
      return;
    }

    if (!isAuthenticated()) {
      window.location.href = '/';
      return;
    }

    const user = getCurrentUser();
    if (!user?.propertyId) {
      window.location.href = '/';
      return;
    }

    const fetchBrief = async () => {
      try {
        const data = await authFetch<BriefResponse>(
          `/briefs/${user.propertyId}/${briefDate}`
        );
        setState({ brief: data, loading: false, error: null });
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : 'Failed to load brief';
        setState({ brief: null, loading: false, error: message });
      }
    };

    fetchBrief();
  }, [briefDate]);

  if (state.loading) {
    return <div className="py-8 text-center text-gray-400">Loading brief...</div>;
  }

  if (state.error || !state.brief) {
    return (
      <div className="py-8 text-center">
        <p className="text-danger mb-4">{state.error || 'Brief not found'}</p>
        <button
          onClick={() => router.back()}
          className="text-sm text-accent"
        >
          &larr; Back to today
        </button>
      </div>
    );
  }

  const { brief } = state;

  return (
    <div className="py-4">
      {/* Back navigation */}
      <button
        onClick={() => router.back()}
        className="flex items-center gap-1 text-sm text-accent mb-4"
      >
        <span>&larr;</span>
        <span>Back</span>
      </button>

      {/* Date header */}
      <h2 className="text-lg font-semibold mb-1">{formatFullDate(briefDate)}</h2>
      <p className="text-[10px] text-gray-500 mb-4">
        Generated {new Date(brief.dailyKPIs.asOf).toLocaleString()}
      </p>

      {/* Audio player (if available) */}
      <AudioBriefPlayer
        audioUrl={brief.audioBrief?.audioUrl}
        durationSeconds={brief.audioBrief?.durationSeconds}
      />

      {/* Narrative text */}
      {(brief as any).narrative && (
        <div className="bg-surface rounded-xl p-4 mb-4">
          <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-2">
            Brief Narrative
          </h3>
          <p className="text-sm text-gray-200 leading-relaxed">
            {(brief as any).narrative}
          </p>
        </div>
      )}

      {/* KPI Grid */}
      <KpiGrid kpis={brief.dailyKPIs} />
    </div>
  );
}
