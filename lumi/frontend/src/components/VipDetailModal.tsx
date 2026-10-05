'use client';

import type { VipArrival } from '@/lib/types';
import { TIER_COLORS, TIER_BG_COLORS } from '@/lib/constants';

interface VipDetailModalProps {
  vip: VipArrival;
  onClose: () => void;
}

export default function VipDetailModal({ vip, onClose }: VipDetailModalProps) {
  const arrivalTime = new Date(vip.estimatedArrival).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });

  return (
    <div className="fixed inset-0 bg-black/70 z-[60] flex items-end" onClick={onClose}>
      <div
        className="bg-surface w-full rounded-t-2xl p-5 max-h-[85vh] overflow-y-auto animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header with close button */}
        <div className="flex justify-between items-start mb-4">
          <div className="flex items-center gap-3">
            <span
              className={`w-14 h-14 rounded-full flex items-center justify-center text-lg font-bold ${TIER_BG_COLORS[vip.loyaltyTier]}/20 ${TIER_COLORS[vip.loyaltyTier]}`}
            >
              {vip.initials}
            </span>
            <div>
              <h2 className="text-lg font-semibold">{vip.guestName}</h2>
              <span className={`text-xs px-2 py-0.5 rounded font-medium ${TIER_BG_COLORS[vip.loyaltyTier]}/20 ${TIER_COLORS[vip.loyaltyTier]}`}>
                {vip.loyaltyTier}
              </span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 text-2xl leading-none p-1"
            aria-label="Close modal"
          >
            &times;
          </button>
        </div>

        {/* Details grid */}
        <div className="grid grid-cols-2 gap-3 mb-4">
          {vip.loyaltyNumber && (
            <DetailItem label="Loyalty #" value={vip.loyaltyNumber} />
          )}
          <DetailItem label="Total Stays" value={String(vip.totalStays)} />
          <DetailItem label="Room" value={`${vip.roomNumber} (${vip.roomType})`} />
          <DetailItem label="Arrival" value={arrivalTime} />
          <DetailItem label="Account" value={vip.accountType} />
          {vip.corporateAccount && (
            <DetailItem label="Corporate" value={vip.corporateAccount} />
          )}
        </div>

        {/* Special occasion badge */}
        {vip.specialOccasion && (
          <div className="mb-4 flex items-center gap-2">
            <span className="text-warning text-sm">&#9733;</span>
            <span className="text-sm text-warning font-medium">
              {vip.specialOccasion.replace(/_/g, ' ')}
            </span>
          </div>
        )}

        {/* Preferences */}
        {vip.preferences.length > 0 && (
          <div className="mb-4">
            <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Preferences</h3>
            <div className="flex flex-wrap gap-1.5">
              {vip.preferences.map((pref) => (
                <span
                  key={pref}
                  className="text-xs px-2 py-1 rounded-lg bg-background text-gray-300"
                >
                  {pref.replace(/_/g, ' ')}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Sensitive notes */}
        {vip.sensitiveNotes && vip.sensitiveNotes.length > 0 && (
          <div className="mb-4 border border-danger/30 rounded-lg p-3 bg-danger/5">
            <h3 className="text-xs text-danger uppercase tracking-wide mb-2">Sensitive Notes</h3>
            <ul className="space-y-1">
              {vip.sensitiveNotes.map((note, idx) => (
                <li key={idx} className="text-xs text-gray-300">
                  &bull; {note}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[10px] text-gray-500 uppercase tracking-wide">{label}</p>
      <p className="text-sm text-gray-200">{value}</p>
    </div>
  );
}
