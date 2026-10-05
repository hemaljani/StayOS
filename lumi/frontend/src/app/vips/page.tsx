'use client';

import { useState } from 'react';
import { useBrief } from '@/hooks/useBrief';
import VipCard from '@/components/VipCard';
import VipDetailModal from '@/components/VipDetailModal';
import { TIER_COLORS } from '@/lib/constants';
import type { VipArrival } from '@/lib/types';

const TIER_ORDER: Record<string, number> = { AMBASSADOR: 0, TITANIUM: 1, PLATINUM: 2 };

export default function VipsPage() {
  const { brief, loading, error } = useBrief();
  const [selectedVip, setSelectedVip] = useState<VipArrival | null>(null);

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading VIP data...</div>;
  }

  if (error || !brief) {
    return <div className="py-8 text-center text-danger">{error || 'Failed to load brief'}</div>;
  }

  // Sort VIPs by tier: Ambassador > Titanium > Platinum
  const sortedVips = [...brief.vipArrivals].sort(
    (a, b) => (TIER_ORDER[a.loyaltyTier] ?? 3) - (TIER_ORDER[b.loyaltyTier] ?? 3)
  );

  const tierCounts = {
    ambassador: brief.vipArrivals.filter((v) => v.loyaltyTier === 'AMBASSADOR').length,
    titanium: brief.vipArrivals.filter((v) => v.loyaltyTier === 'TITANIUM').length,
    platinum: brief.vipArrivals.filter((v) => v.loyaltyTier === 'PLATINUM').length,
  };

  return (
    <div className="py-4">
      <h2 className="text-lg font-semibold mb-1">VIP Arrivals</h2>

      {/* Header summary */}
      <div className="flex items-center gap-2 mb-4">
        <span className="text-sm text-gray-400">{brief.vipArrivals.length} guests</span>
        <span className="text-gray-600">&middot;</span>
        <span className={`text-xs ${TIER_COLORS.AMBASSADOR}`}>
          {tierCounts.ambassador} Amb
        </span>
        <span className={`text-xs ${TIER_COLORS.TITANIUM}`}>
          {tierCounts.titanium} Tit
        </span>
        <span className={`text-xs ${TIER_COLORS.PLATINUM}`}>
          {tierCounts.platinum} Plat
        </span>
      </div>

      {/* VIP list */}
      <div className="space-y-2">
        {sortedVips.map((vip) => (
          <VipCard key={vip.guestId} vip={vip} onTap={() => setSelectedVip(vip)} />
        ))}
      </div>

      {/* Detail modal */}
      {selectedVip && (
        <VipDetailModal vip={selectedVip} onClose={() => setSelectedVip(null)} />
      )}
    </div>
  );
}
