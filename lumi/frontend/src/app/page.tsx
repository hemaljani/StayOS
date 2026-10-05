'use client';

import { useState } from 'react';
import { useBrief } from '@/hooks/useBrief';
import { useBriefHistory } from '@/hooks/useBriefHistory';
import { useSettings } from '@/hooks/useSettings';
import AudioBriefPlayer from '@/components/AudioBriefPlayer';
import KpiGrid from '@/components/KpiGrid';
import ActionItemsList from '@/components/ActionItemsList';
import PastBriefsList from '@/components/PastBriefsList';
import WalkStrategyModal from '@/components/WalkStrategyModal';
import Link from 'next/link';
import { TIER_COLORS } from '@/lib/constants';
import ThemeToggle from '@/components/ThemeToggle';

export default function DailyBriefPage() {
  const { brief, loading, error } = useBrief();
  const { history } = useBriefHistory();
  const { settings } = useSettings();
  const [showWalkStrategy, setShowWalkStrategy] = useState(false);

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading brief...</div>;
  }

  if (error || !brief) {
    return <div className="py-8 text-center text-danger">{error || 'Failed to load brief'}</div>;
  }

  const overbookingItem = brief.actionItems.find((i) => i.type === 'OVERBOOKING_RISK');
  const topVips = brief.vipArrivals.slice(0, 4);

  // Extract first name from full GM name (e.g., "Jennifer Smith" -> "Jennifer")
  const firstName = settings?.gmName?.split(' ')[0];

  return (
    <div className="py-4">
      {/* Top row: GM greeting (left) + light/dark theme toggle (top-right). */}
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          {firstName && <h1 className="text-xl font-semibold">Hello, {firstName}</h1>}
          {settings?.propertyName && (
            <p className="text-sm text-gray-400">{settings.propertyName}</p>
          )}
        </div>
        <ThemeToggle className="flex-shrink-0" />
      </div>
      {/* Data freshness indicator */}
      <p className="text-[10px] text-gray-500 mb-3">
        Data as of {new Date(brief.dailyKPIs.asOf).toLocaleString()}
      </p>

      {/* Audio player */}
      <AudioBriefPlayer
        audioUrl={brief.audioBrief.audioUrl}
        durationSeconds={brief.audioBrief.durationSeconds}
      />

      {/* KPI metrics */}
      <KpiGrid kpis={brief.dailyKPIs} />

      {/* Action Items */}
      <ActionItemsList
        items={brief.actionItems}
        onOverbookingTap={() => setShowWalkStrategy(true)}
      />

      {/* VIP Preview */}
      <div className="mb-4">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-gray-300">VIP Arrivals</h3>
          <Link href="/vips/" className="text-xs text-accent">View All &rarr;</Link>
        </div>
        <div className="space-y-2">
          {topVips.map((vip) => {
            const tierKey = vip.loyaltyTier.toLowerCase() as 'ambassador' | 'titanium' | 'platinum';
            const bgClass = tierKey === 'ambassador'
              ? 'bg-tier-ambassador/20'
              : tierKey === 'titanium'
                ? 'bg-tier-titanium/20'
                : 'bg-tier-platinum/20';

            // Show max 3 preferences on mobile, with overflow indicator
            const visiblePrefs = vip.preferences.slice(0, 3);
            const overflowCount = vip.preferences.length - 3;

            return (
              <div key={vip.guestId} className="bg-surface rounded-lg p-3 flex items-center gap-3">
                <span className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold ${bgClass} ${TIER_COLORS[vip.loyaltyTier]}`}>
                  {vip.initials}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{vip.guestName}</p>
                  <p className="text-xs text-gray-400">
                    Rm {vip.roomNumber} &middot; {vip.roomType} &middot; {new Date(vip.estimatedArrival).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </p>
                  {/* Preferences: max 3 visible with +N overflow */}
                  <div className="flex gap-1 mt-1 flex-wrap">
                    {visiblePrefs.map((pref) => (
                      <span
                        key={pref}
                        className="text-[10px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-300"
                      >
                        {pref}
                      </span>
                    ))}
                    {overflowCount > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-400">
                        +{overflowCount} more
                      </span>
                    )}
                  </div>
                </div>
                <span className={`text-[10px] px-1.5 py-0.5 rounded ${TIER_COLORS[vip.loyaltyTier]}`}>
                  {vip.loyaltyTier}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Past Briefs */}
      {history.length > 0 && <PastBriefsList history={history} />}

      {/* Walk Strategy Modal */}
      {showWalkStrategy && overbookingItem && (
        <WalkStrategyModal
          actionItem={overbookingItem}
          onClose={() => setShowWalkStrategy(false)}
        />
      )}
    </div>
  );
}
