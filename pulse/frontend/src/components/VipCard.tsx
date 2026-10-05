// VipCard - a single VIP arrival row on the VIPs tab (Task 21.2, Requirement 15.9).
//
// Shows the guest's tier-colored avatar (initials), name with a tier pill, a
// compact subline (room, stays, special occasion), and their estimated arrival.
// Selecting the card opens the VIP profile modal (Requirement 15.10). Rendered
// as an accessible button so it is keyboard-operable.

import type { VipGuest } from '@/lib/types';
import {
  VIP_TIER_AVATAR_COLORS,
  VIP_TIER_PILL_COLORS,
  VIP_TIER_FALLBACK_AVATAR,
  VIP_TIER_FALLBACK_PILL,
  vipTierLabel,
} from '@/lib/constants';
import { initialsFor } from '@/lib/format';

interface VipCardProps {
  guest: VipGuest;
  onSelect: (guest: VipGuest) => void;
}

export default function VipCard({ guest, onSelect }: VipCardProps) {
  const tier = String(guest.loyaltyTier ?? 'UNKNOWN');
  const avatarClass = VIP_TIER_AVATAR_COLORS[tier] ?? VIP_TIER_FALLBACK_AVATAR;
  const pillClass = VIP_TIER_PILL_COLORS[tier] ?? VIP_TIER_FALLBACK_PILL;
  const initials = initialsFor(guest.guestName, guest.initials);

  // Compose a compact subline from the fields that are present.
  const sublineParts = [
    guest.roomNumber ? `Rm ${guest.roomNumber}` : null,
    typeof guest.totalStays === 'number' ? `${guest.totalStays} stays` : null,
    guest.specialOccasion || null,
    guest.corporateAccount || null,
  ].filter(Boolean);

  return (
    <button
      type="button"
      onClick={() => onSelect(guest)}
      className="w-full text-left bg-surface rounded-xl border border-gray-800 p-3 flex items-center gap-3 active:scale-[0.99] transition-transform"
    >
      {/* Tier-colored avatar with initials */}
      <span
        aria-hidden
        className={`flex items-center justify-center w-9 h-9 rounded-full text-xs font-extrabold text-white shrink-0 ${avatarClass}`}
      >
        {initials}
      </span>

      {/* Name + subline */}
      <span className="flex-1 min-w-0">
        <span className="flex items-center gap-1.5">
          <span className="text-sm font-semibold text-ink truncate">
            {guest.guestName ?? 'VIP Guest'}
          </span>
          <span className={`text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded-full ${pillClass}`}>
            {vipTierLabel(tier)}
          </span>
        </span>
        {sublineParts.length > 0 && (
          <span className="block text-[11px] text-gray-500 mt-0.5 truncate">
            {sublineParts.join(' \u00b7 ')}
          </span>
        )}
      </span>

      {/* Estimated arrival */}
      {guest.estimatedArrival && (
        <span className="text-xs font-semibold text-accent whitespace-nowrap">
          {guest.estimatedArrival}
        </span>
      )}
    </button>
  );
}
