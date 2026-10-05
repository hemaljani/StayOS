'use client';

interface OooRoom {
  roomNumber: string;
  issue: string;
  workOrderId: string;
  openHours: number;
  isPremium: boolean;
  view?: string;
}

interface OooDetailModalProps {
  room: OooRoom;
  onClose: () => void;
}

export default function OooDetailModal({ room, onClose }: OooDetailModalProps) {
  const hours = room.openHours ?? 0;
  const urgencyColor =
    hours > 24 ? 'text-danger' : hours > 12 ? 'text-warning' : 'text-success';

  return (
    <div className="fixed inset-0 bg-black/70 z-[60] flex items-end" onClick={onClose}>
      <div
        className="bg-surface w-full rounded-t-2xl p-5 max-h-[85vh] overflow-y-auto animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header with room number and close button */}
        <div className="flex justify-between items-start mb-4">
          <div className="flex items-center gap-3">
            <div className="w-14 h-14 rounded-lg bg-danger/10 flex items-center justify-center">
              <span className="text-lg font-bold text-danger">{room.roomNumber}</span>
            </div>
            <div>
              <h2 className="text-lg font-semibold text-gray-200">Room {room.roomNumber}</h2>
              {room.isPremium && (
                <span className="text-[10px] px-2 py-0.5 rounded bg-warning/20 text-warning font-medium">
                  PREMIUM
                </span>
              )}
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

        {/* Divider */}
        <div className="border-t border-gray-700 mb-4" />

        {/* Details grid */}
        <div className="space-y-3 mb-4">
          <DetailRow label="Issue Type" value={room.issue} />
          <DetailRow label="Work Order" value={room.workOrderId} />
          <DetailRow
            label="Status"
            value="OPEN"
            badge
            badgeClass="bg-warning/20 text-warning"
          />
          <DetailRow
            label="Hours Open"
            value={`${hours}h`}
            valueClass={urgencyColor}
          />
          <DetailRow
            label="View"
            value={room.view ? room.view.charAt(0).toUpperCase() + room.view.slice(1) : 'Standard'}
          />
        </div>

        {/* Divider */}
        <div className="border-t border-gray-700 mb-4" />

        {/* Note */}
        <div className="bg-background rounded-lg p-3">
          <p className="text-xs text-gray-400">Contact Engineering for ETA</p>
        </div>
      </div>
    </div>
  );
}

function DetailRow({
  label,
  value,
  valueClass,
  badge,
  badgeClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
  badge?: boolean;
  badgeClass?: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-gray-500 uppercase tracking-wide">{label}</span>
      {badge ? (
        <span className={`text-xs px-2 py-0.5 rounded font-medium ${badgeClass}`}>{value}</span>
      ) : (
        <span className={`text-sm font-medium ${valueClass || 'text-gray-200'}`}>{value}</span>
      )}
    </div>
  );
}
