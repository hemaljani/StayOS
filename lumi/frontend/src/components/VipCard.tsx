import type { VipArrival } from '@/lib/types';
import { TIER_COLORS, TIER_BG_COLORS } from '@/lib/constants';

interface VipCardProps {
  vip: VipArrival;
  onTap: () => void;
}

export default function VipCard({ vip, onTap }: VipCardProps) {
  const arrivalTime = new Date(vip.estimatedArrival).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });

  return (
    <button
      onClick={onTap}
      className="w-full bg-surface rounded-xl p-3 flex items-center gap-3 text-left active:scale-[0.98] transition-transform"
    >
      {/* Tier-colored initials avatar */}
      <span
        className={`w-10 h-10 rounded-full flex items-center justify-center text-sm font-bold shrink-0 ${TIER_BG_COLORS[vip.loyaltyTier]}/20 ${TIER_COLORS[vip.loyaltyTier]}`}
      >
        {vip.initials}
      </span>

      {/* Guest info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <p className="text-sm font-medium truncate">{vip.guestName}</p>
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${TIER_BG_COLORS[vip.loyaltyTier]}/20 ${TIER_COLORS[vip.loyaltyTier]}`}>
            {vip.loyaltyTier}
          </span>
        </div>
        <p className="text-xs text-gray-400 mt-0.5">
          Rm {vip.roomNumber} &middot; {vip.roomType} &middot; {arrivalTime}
        </p>
        {/* Preference chips */}
        {vip.preferences.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1.5">
            {vip.preferences.slice(0, 3).map((pref) => (
              <span
                key={pref}
                className="text-[9px] px-1.5 py-0.5 rounded bg-background text-gray-400"
              >
                {pref.replace(/_/g, ' ')}
              </span>
            ))}
            {vip.preferences.length > 3 && (
              <span className="text-[9px] px-1.5 py-0.5 text-gray-500">
                +{vip.preferences.length - 3}
              </span>
            )}
          </div>
        )}
      </div>
    </button>
  );
}
