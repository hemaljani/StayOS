// VipProfileModal - VIP guest profile detail (Task 21.2, Requirement 15.10).
//
// Opened when a GM selects a VIP card. Shows the guest's avatar, name, tier, a
// stats row (total stays, room, arrival), and their preferences and profile
// notes (special occasion, account/corporate, loyalty number). Mirrors the
// TriageModal's bottom-sheet interaction (Escape to close, click-outside to
// dismiss, focus-trapping dialog semantics) so the tabs feel consistent.
//
// sensitiveNotes are never present in the payload (stripped server-side,
// Requirement 16.6), so there is nothing sensitive to render here.

'use client';

import { useEffect } from 'react';
import { X } from 'lucide-react';
import type { VipGuest } from '@/lib/types';
import {
  VIP_TIER_AVATAR_COLORS,
  VIP_TIER_PILL_COLORS,
  VIP_TIER_FALLBACK_AVATAR,
  VIP_TIER_FALLBACK_PILL,
  vipTierLabel,
} from '@/lib/constants';
import { formatVipArrival, initialsFor } from '@/lib/format';

interface VipProfileModalProps {
  guest: VipGuest;
  propertyId?: string;
  onClose: () => void;
}

export default function VipProfileModal({ guest, propertyId, onClose }: VipProfileModalProps) {
  const tier = String(guest.loyaltyTier ?? 'UNKNOWN');
  const avatarClass = VIP_TIER_AVATAR_COLORS[tier] ?? VIP_TIER_FALLBACK_AVATAR;
  const pillClass = VIP_TIER_PILL_COLORS[tier] ?? VIP_TIER_FALLBACK_PILL;
  const initials = initialsFor(guest.guestName, guest.initials);
  const arrival = formatVipArrival(guest.estimatedArrival, propertyId);

  // Close on Escape for keyboard accessibility.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // The profile "notes" rows: only rendered when the corresponding field exists.
  const noteRows: { key: string; value: string }[] = [
    guest.specialOccasion ? { key: 'Occasion', value: guest.specialOccasion } : null,
    guest.accountType ? { key: 'Account', value: guest.accountType } : null,
    guest.corporateAccount ? { key: 'Corporate', value: guest.corporateAccount } : null,
    guest.loyaltyNumber ? { key: 'Loyalty #', value: guest.loyaltyNumber } : null,
    guest.roomType ? { key: 'Room type', value: guest.roomType } : null,
  ].filter((row): row is { key: string; value: string } => row !== null);

  return (
    <div
      className="fixed inset-0 bg-black/70 z-[60] flex items-end"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="vip-modal-title"
        className="bg-surface w-full max-w-md mx-auto rounded-t-2xl p-5 max-h-[88vh] overflow-y-auto animate-slide-up"
        onClick={(event) => event.stopPropagation()}
      >
        {/* Close control */}
        <div className="flex justify-end -mt-1 -mr-1">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-gray-400 hover:text-ink p-1"
          >
            <X size={20} />
          </button>
        </div>

        {/* Avatar + identity */}
        <div className="text-center">
          <span
            aria-hidden
            className={`inline-flex items-center justify-center w-14 h-14 rounded-full text-lg font-black mb-2 ${avatarClass}`}
          >
            {initials}
          </span>
          <h2 id="vip-modal-title" className="text-base font-bold text-ink">
            {guest.guestName ?? 'VIP Guest'}
          </h2>
          <span className={`inline-block text-[9px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full mt-1 ${pillClass}`}>
            {vipTierLabel(tier)}
          </span>
        </div>

        {/* Stats row */}
        <div className="grid grid-cols-3 gap-2 mt-4 mb-4">
          <ProfileStat label="Stays" value={typeof guest.totalStays === 'number' ? String(guest.totalStays) : '--'} />
          <ProfileStat label="Room" value={guest.roomNumber ?? '--'} />
          <ProfileStat label="Arrival" value={arrival.time} title={arrival.detail} />
        </div>

        {/* Preferences (Requirement 15.10) */}
        <section aria-label="Guest preferences" className="border-t border-gray-800 pt-4">
          <h3 className="text-[10px] uppercase tracking-wide text-gray-500 mb-2">Preferences</h3>
          {guest.preferences && guest.preferences.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {guest.preferences.map((preference) => (
                <span
                  key={preference}
                  className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-surface-2 text-gray-300"
                >
                  {preference}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-xs text-gray-500">No recorded preferences.</p>
          )}
        </section>

        {/* Profile notes */}
        {noteRows.length > 0 && (
          <section aria-label="Profile notes" className="border-t border-gray-800 pt-4 mt-4">
            <h3 className="text-[10px] uppercase tracking-wide text-gray-500 mb-2">Profile</h3>
            <dl className="space-y-1.5">
              {noteRows.map((row) => (
                <div key={row.key} className="flex justify-between gap-3">
                  <dt className="text-xs text-gray-500">{row.key}</dt>
                  <dd className="text-xs font-medium text-ink text-right">{row.value}</dd>
                </div>
              ))}
            </dl>
          </section>
        )}
      </div>
    </div>
  );
}

// A single stat tile in the profile header row.
function ProfileStat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="bg-surface-2 rounded-lg p-2 text-center">
      <p title={title} className="text-sm font-bold text-ink tabular-nums truncate">{value}</p>
      <p className="text-[9px] uppercase tracking-wide text-gray-500 mt-0.5">{label}</p>
    </div>
  );
}
