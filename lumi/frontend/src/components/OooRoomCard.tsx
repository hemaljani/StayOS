interface OooRoomCardProps {
  roomNumber: string;
  issueType: string;
  workOrderId: string;
  hoursOpen: number;
  isPremium: boolean;
  view?: string;
  onClick?: () => void;
}

export default function OooRoomCard({
  roomNumber,
  issueType,
  workOrderId,
  hoursOpen,
  isPremium,
  onClick,
}: OooRoomCardProps) {
  return (
    <div
      className={`bg-surface rounded-xl p-3 relative cursor-pointer hover:bg-surface/80 ${isPremium ? 'border border-warning/40' : ''}`}
      onClick={onClick}
    >
      {/* Premium badge */}
      {isPremium && (
        <span className="absolute top-2 right-2 text-[9px] px-1.5 py-0.5 rounded bg-warning/20 text-warning font-medium">
          PREMIUM
        </span>
      )}

      <div className="flex items-center gap-3">
        {/* Room number */}
        <div className="w-12 h-12 rounded-lg bg-danger/10 flex items-center justify-center shrink-0">
          <span className="text-sm font-bold text-danger">{roomNumber}</span>
        </div>

        {/* Details */}
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-gray-200">{issueType}</p>
          <p className="text-xs text-gray-500 mt-0.5">WO# {workOrderId}</p>
        </div>

        {/* Hours open badge */}
        <div className="text-center shrink-0">
          <span className={`text-xs font-bold ${(hoursOpen || 0) > 24 ? 'text-danger' : 'text-warning'}`}>
            {hoursOpen || 0}h
          </span>
          <p className="text-[9px] text-gray-500">open</p>
        </div>
      </div>
    </div>
  );
}
