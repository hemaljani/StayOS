// VIPs tab - VIP arrivals grouped by loyalty tier (Requirement 15.9, 15.10).
//
// Fetches GET /vips (via useVips) and renders arrivals grouped by tier in
// eliteness order (the facade already orders them AMBASSADOR > TITANIUM >
// PLATINUM). Each guest card opens a profile modal showing preferences and
// profile fields (Requirement 15.10). Loading, empty, and error states mirror
// the PULSE tab so the tabs behave consistently.

'use client';

import { useState } from 'react';
import { Star } from 'lucide-react';
import { useVips } from '@/hooks/useVips';
import VipCard from '@/components/VipCard';
import VipProfileModal from '@/components/VipProfileModal';
import { vipTierLabel } from '@/lib/constants';
import type { VipGuest } from '@/lib/types';

export default function VipsPage() {
  const { data, loading, error, refetch } = useVips();
  const [selectedGuest, setSelectedGuest] = useState<VipGuest | null>(null);

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading VIP arrivals...</div>;
  }

  if (error) {
    return (
      <div className="py-8 text-center">
        <p className="text-danger mb-3">{error}</p>
        <button type="button" onClick={() => refetch()} className="text-sm text-accent underline">
          Try again
        </button>
      </div>
    );
  }

  const tiers = data?.tiers ?? [];
  const vipCount = data?.vipCount ?? 0;

  return (
    <div className="py-4">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">VIP Arrivals</h2>
        <span className="text-[10px] font-bold px-2 py-1 rounded-full bg-accent-secondary/15 text-accent-secondary">
          {vipCount} today
        </span>
      </div>

      {/* Empty state */}
      {tiers.length === 0 ? (
        <div className="bg-surface rounded-xl p-8 text-center border border-gray-800">
          <Star size={28} className="mx-auto text-accent mb-3" strokeWidth={1.5} aria-hidden />
          <p className="text-sm text-gray-400">No VIP arrivals scheduled today.</p>
        </div>
      ) : (
        <div className="space-y-5">
          {tiers.map((group) => (
            <section key={group.tier} aria-label={`${vipTierLabel(group.tier)} arrivals`}>
              {/* Tier heading */}
              <div className="flex items-center gap-2 mb-2">
                <h3 className="text-[10px] font-bold uppercase tracking-wide text-gray-400">
                  {vipTierLabel(group.tier)}
                </h3>
                <span className="text-[10px] text-gray-600 tabular-nums">({group.count})</span>
                <span className="flex-1 h-px bg-gray-800" />
              </div>

              {/* Guest cards */}
              <div className="space-y-2">
                {group.guests.map((guest) => (
                  <VipCard key={guest.guestId} guest={guest} onSelect={setSelectedGuest} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      {/* Profile modal */}
      {selectedGuest && (
        <VipProfileModal guest={selectedGuest} onClose={() => setSelectedGuest(null)} />
      )}
    </div>
  );
}
