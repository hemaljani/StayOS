'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import type { BriefHistorySummary } from '@/lib/types';

interface PastBriefsListProps {
  history: BriefHistorySummary[];
}

/**
 * Returns an occupancy badge color class based on threshold:
 * green >= 85%, warning 75-84%, red < 75%
 */
function getOccBadgeClasses(occ: number): string {
  if (occ >= 85) return 'bg-success/20 text-success';
  if (occ >= 75) return 'bg-warning/20 text-warning';
  return 'bg-danger/20 text-danger';
}

/**
 * Formats a briefDate string (YYYY-MM-DD) as "Mon 7/14" style
 */
function formatDate(briefDate: string): string {
  const date = new Date(briefDate + 'T00:00:00');
  const weekday = date.toLocaleDateString('en-US', { weekday: 'short' });
  const month = date.getMonth() + 1;
  const day = date.getDate();
  return `${weekday} ${month}/${day}`;
}

export default function PastBriefsList({ history }: PastBriefsListProps) {
  const [expanded, setExpanded] = useState(false);
  const router = useRouter();

  // Filter out today's entry
  const today = new Date().toISOString().slice(0, 10);
  const pastItems = history
    .filter((item) => item.briefDate !== today)
    .reverse() // most recent first
    .slice(0, 6);

  if (pastItems.length === 0) return null;

  const handleBriefClick = (briefDate: string) => {
    router.push(`/brief/?date=${briefDate}`);
  };

  return (
    <div className="mb-4">
      {/* Toggle button */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-sm text-accent font-medium mb-2"
      >
        <span className="text-xs">{expanded ? '▾' : '▸'}</span>
        {expanded ? 'Hide past briefs' : 'Show past briefs'}
      </button>

      {/* Expandable list */}
      {expanded && (
        <div className="space-y-2">
          {pastItems.map((item) => {
            const occ = item.dailyKPIs.occupancy.current;
            const preview =
              item.narrativePreview.length > 40
                ? item.narrativePreview.slice(0, 40) + '...'
                : item.narrativePreview;

            return (
              <div
                key={item.briefDate}
                className="bg-surface rounded-lg p-3 cursor-pointer hover:bg-surface/80 transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 focus:ring-offset-1 focus:ring-offset-background"
                onClick={() => handleBriefClick(item.briefDate)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    handleBriefClick(item.briefDate);
                  }
                }}
                role="button"
                tabIndex={0}
                aria-label={`View brief for ${formatDate(item.briefDate)}`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-400 w-16 shrink-0">
                    {formatDate(item.briefDate)}
                  </span>
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${getOccBadgeClasses(occ)}`}
                  >
                    {occ}%
                  </span>
                  <span className="text-xs text-gray-300">${item.dailyKPIs.adr.current}</span>
                  <span className="text-xs text-gray-500 flex-1 truncate">{preview}</span>
                  <span className="text-xs text-gray-600">&rsaquo;</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
