'use client';

import { useState } from 'react';
import type { ActionItem } from '@/lib/types';

interface WalkStrategyModalProps {
  actionItem: ActionItem;
  onClose: () => void;
}

interface CompanionProperty {
  propertyId: string;
  propertyName: string;
  availableRooms: number;
}

const WALK_STEPS = [
  'Identify guests eligible for walk (lowest tier, latest booking)',
  'Contact selected companion property to confirm availability',
  'Prepare walk package: complimentary transport + amenity voucher',
  'Notify front desk team with guest list and walk assignments',
  'Contact affected guests with upgrade offer at companion property',
  'Update reservation system with walk status and companion booking',
];

export default function WalkStrategyModal({ actionItem, onClose }: WalkStrategyModalProps) {
  const [selectedCompanion, setSelectedCompanion] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);

  // Extract walk strategy data from action item
  const confirmedCount = Number(actionItem.data?.confirmedCount || 0);
  const availableRooms = Number(actionItem.data?.availableRooms || 0);
  const overage = confirmedCount - availableRooms;
  const companions: CompanionProperty[] = (actionItem.data?.walkStrategy as CompanionProperty[]) || [];

  if (dismissed) {
    return null;
  }

  const handleConfirm = () => {
    setDismissed(true);
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/70 z-[60] flex items-end" onClick={onClose}>
      <div
        className="bg-surface w-full rounded-t-2xl p-5 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-semibold">Walk Strategy</h2>
          <button
            onClick={onClose}
            className="text-gray-400 text-2xl leading-none p-1"
            aria-label="Close modal"
          >
            &times;
          </button>
        </div>

        {/* Overage display */}
        <div className="bg-danger/10 border border-danger/30 rounded-xl p-4 mb-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs text-gray-400">Confirmed Reservations</p>
              <p className="text-xl font-bold text-ink">{confirmedCount}</p>
            </div>
            <div className="text-center">
              <p className="text-xs text-gray-400">Available Rooms</p>
              <p className="text-xl font-bold text-ink">{availableRooms}</p>
            </div>
            <div className="text-center">
              <p className="text-xs text-danger">Overage</p>
              <p className="text-xl font-bold text-danger">+{overage > 0 ? overage : 0}</p>
            </div>
          </div>
        </div>

        {/* Companion property selection */}
        {companions.length > 0 && (
          <div className="mb-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-2">Companion Properties</h3>
            <div className="space-y-2">
              {companions.map((companion) => (
                <label
                  key={companion.propertyId}
                  className={`flex items-center gap-3 p-3 rounded-xl cursor-pointer transition-colors ${
                    selectedCompanion === companion.propertyId
                      ? 'bg-accent/10 border border-accent/40'
                      : 'bg-background border border-transparent'
                  }`}
                >
                  <input
                    type="radio"
                    name="companion"
                    value={companion.propertyId}
                    checked={selectedCompanion === companion.propertyId}
                    onChange={() => setSelectedCompanion(companion.propertyId)}
                    className="accent-accent w-4 h-4 shrink-0"
                  />
                  <div className="flex-1">
                    <p className="text-sm text-gray-200">{companion.propertyName}</p>
                    <p className="text-[10px] text-gray-500">{companion.propertyId}</p>
                  </div>
                  <span className="text-xs text-success font-medium">
                    {companion.availableRooms} avail
                  </span>
                </label>
              ))}
            </div>
          </div>
        )}

        {/* Walk strategy workflow steps */}
        <div className="mb-5">
          <h3 className="text-sm font-semibold text-gray-300 mb-3">Walk Workflow</h3>
          <div className="space-y-3">
            {WALK_STEPS.map((step, idx) => (
              <div key={idx} className="flex items-start gap-3">
                <span className="w-6 h-6 rounded-full bg-accent/20 text-accent text-xs font-bold flex items-center justify-center shrink-0 mt-0.5">
                  {idx + 1}
                </span>
                <p className="text-xs text-gray-300 leading-relaxed">{step}</p>
              </div>
            ))}
          </div>
        </div>

        {/* Confirm button */}
        <button
          onClick={handleConfirm}
          disabled={companions.length > 0 && !selectedCompanion}
          className="w-full bg-accent text-white font-medium py-3 rounded-xl disabled:opacity-40 active:scale-[0.98] transition-transform"
        >
          Confirm Walk Strategy
        </button>
        <p className="text-[10px] text-gray-500 text-center mt-2">
          UI confirmation only - no changes to source systems
        </p>
      </div>
    </div>
  );
}
