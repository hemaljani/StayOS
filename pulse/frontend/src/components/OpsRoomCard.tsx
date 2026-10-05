// OpsRoomCard - a single out-of-order room card on the Ops tab (Task 21.2).
//
// Shows the room number, room type, an OOO status dot, and its joined work-order
// status: priority, issue type, assignee, and estimated resolution. When the OOO
// room has no linked work order it shows an explicit "no work order" tag so the
// GM can see the gap (Requirement 15.11). Premium rooms and the OOO status are
// surfaced as tags matching the prototype.

import type { OooRoom } from '@/lib/types';

interface OpsRoomCardProps {
  room: OooRoom;
}

// Map a work-order status to a status-dot color. Open/unassigned reads as
// critical, in-progress as warning, anything else as neutral success.
function statusDotClass(status: string | undefined): string {
  const value = (status ?? '').toUpperCase();
  if (value.includes('OPEN') || value.includes('NEW') || value.includes('UNASSIGNED')) {
    return 'bg-tier-critical';
  }
  if (value.includes('PROGRESS')) return 'bg-tier-warning';
  return 'bg-success';
}

export default function OpsRoomCard({ room }: OpsRoomCardProps) {
  const workOrder = room.workOrder;

  // Tags summarizing the work-order status (or its absence).
  const tags: { label: string; tone: 'neutral' | 'premium' | 'urgent' | 'ok' }[] = [];
  if (room.isPremiumRoom) tags.push({ label: room.view || 'Premium', tone: 'premium' });
  if (workOrder) {
    if (workOrder.priority) {
      const urgent = workOrder.priority.toUpperCase().includes('HIGH') || workOrder.priority.toUpperCase().includes('URGENT');
      tags.push({ label: `${workOrder.priority} priority`, tone: urgent ? 'urgent' : 'neutral' });
    }
    if (workOrder.assignedTo) tags.push({ label: workOrder.assignedTo, tone: 'neutral' });
    if (typeof workOrder.estimatedResolutionHours === 'number') {
      tags.push({ label: `ETA ${workOrder.estimatedResolutionHours}h`, tone: 'neutral' });
    }
    if (workOrder.workOrderId) tags.push({ label: `WO ${workOrder.workOrderId}`, tone: 'neutral' });
    if (workOrder.status) {
      tags.push({ label: workOrder.status, tone: workOrder.status.toUpperCase().includes('PROGRESS') ? 'ok' : 'neutral' });
    }
  } else {
    tags.push({ label: 'No work order', tone: 'urgent' });
  }

  const toneClass: Record<string, string> = {
    neutral: 'bg-surface-2 text-gray-400',
    premium: 'bg-warning/10 text-warning',
    urgent: 'bg-tier-critical/10 text-tier-critical',
    ok: 'bg-success/10 text-success',
  };

  return (
    <div className="bg-surface rounded-xl border border-gray-800 p-3">
      {/* Room header: number, issue/type, status dot */}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-sm font-extrabold text-ink">Rm {room.roomNumber ?? '--'}</span>
        <span className="flex-1 text-xs text-gray-400 truncate">
          {workOrder?.issueType || room.roomType || room.status || 'Out of order'}
        </span>
        <span
          aria-hidden
          className={`w-2 h-2 rounded-full shrink-0 ${statusDotClass(workOrder?.status)}`}
        />
      </div>

      {/* Status tags */}
      <div className="flex flex-wrap gap-1.5">
        {tags.map((tag, index) => (
          <span
            key={`${tag.label}-${index}`}
            className={`text-[9px] font-semibold px-2 py-0.5 rounded-full ${toneClass[tag.tone]}`}
          >
            {tag.label}
          </span>
        ))}
      </div>
    </div>
  );
}
