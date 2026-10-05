'use client';

import { useState } from 'react';
import { useBrief } from '@/hooks/useBrief';
import OooRoomCard from '@/components/OooRoomCard';
import OooDetailModal from '@/components/OooDetailModal';

interface OooRoom {
  roomNumber: string;
  issue: string;
  workOrderId: string;
  openHours: number;
  isPremium: boolean;
  view?: string;
}

export default function OpsPage() {
  const { brief, loading, error } = useBrief();
  const [selectedRoom, setSelectedRoom] = useState<OooRoom | null>(null);

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading ops data...</div>;
  }

  if (error || !brief) {
    return <div className="py-8 text-center text-danger">{error || 'Failed to load brief'}</div>;
  }

  const { actionItems, dailyKPIs } = brief;

  // Filter OOO rooms from action items
  const oooItems = actionItems.filter((item) => item.type === 'ROOMS_OUT_OF_ORDER');
  const oooRooms: OooRoom[] = oooItems.flatMap((item) => {
    // Extract rooms array from action item data, or build a single card from the item
    const rooms = item.data?.rooms as Array<{
      roomNumber: string;
      issue: string;
      workOrderId: string;
      openHours: number;
      isPremium: boolean;
      view?: string;
    }> | undefined;

    if (rooms && Array.isArray(rooms)) {
      return rooms;
    }

    // Fallback: use the action item fields directly
    return [{
      roomNumber: String(item.data?.roomNumber || 'N/A'),
      issue: String(item.data?.issue || item.title),
      workOrderId: String(item.data?.workOrderId || 'N/A'),
      openHours: Number(item.data?.openHours || 0),
      isPremium: Boolean(item.data?.isPremium),
      view: item.data?.view ? String(item.data.view) : undefined,
    }];
  });

  // Filter staffing/group checkout items
  const staffingItems = actionItems.filter((item) => item.type === 'STAFFING_CONFIRMED');

  return (
    <div className="py-4">
      <h2 className="text-lg font-semibold mb-1">Ops / Facilities</h2>
      <p className="text-[10px] text-gray-500 mb-4">
        Data as of {new Date(dailyKPIs.asOf).toLocaleString()}
      </p>

      {/* OOO Rooms Section */}
      <div className="mb-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-2">
          Out-of-Order Rooms ({oooRooms.length})
        </h3>
        {oooRooms.length > 0 ? (
          <div className="space-y-2">
            {oooRooms.map((room, idx) => (
              <OooRoomCard
                key={`${room.roomNumber}-${idx}`}
                roomNumber={room.roomNumber}
                issueType={room.issue}
                workOrderId={room.workOrderId}
                hoursOpen={room.openHours}
                isPremium={room.isPremium}
                view={room.view}
                onClick={() => setSelectedRoom(room)}
              />
            ))}
          </div>
        ) : (
          <div className="bg-surface rounded-xl p-4 text-center">
            <p className="text-xs text-gray-500">No rooms out of order today.</p>
          </div>
        )}
      </div>

      {/* Group Checkout Summary */}
      <div className="mb-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-2">Group Checkouts</h3>
        {dailyKPIs.departures.groupCheckouts > 0 ? (
          <div className="bg-surface rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm text-gray-200">
                {dailyKPIs.departures.groupCheckouts} group{dailyKPIs.departures.groupCheckouts > 1 ? 's' : ''}
              </span>
              <span className="text-xs text-gray-400">
                {dailyKPIs.departures.groupRooms} rooms
              </span>
            </div>
            {staffingItems.map((item) => (
              <div key={item.id} className="mt-2 p-2 rounded-lg bg-background">
                <p className="text-xs text-gray-300">{item.title}</p>
                <p className="text-[10px] text-gray-500 mt-0.5">{item.detail}</p>
              </div>
            ))}
          </div>
        ) : (
          <div className="bg-surface rounded-xl p-4 text-center">
            <p className="text-xs text-gray-500">No group checkouts today.</p>
          </div>
        )}
      </div>

      {/* Staffing Status */}
      {staffingItems.length > 0 && (
        <div className="mb-4">
          <h3 className="text-sm font-semibold text-gray-300 mb-2">Staffing Status</h3>
          <div className="space-y-2">
            {staffingItems.map((item) => (
              <div key={item.id} className="bg-surface rounded-xl p-3 flex items-center gap-3">
                <span className="w-8 h-8 rounded-full bg-success/20 flex items-center justify-center">
                  <span className="text-success text-sm">&#10003;</span>
                </span>
                <div className="flex-1">
                  <p className="text-sm text-gray-200">{item.title}</p>
                  <p className="text-[10px] text-gray-500">{item.detail}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* OOO Detail Modal */}
      {selectedRoom && (
        <OooDetailModal room={selectedRoom} onClose={() => setSelectedRoom(null)} />
      )}
    </div>
  );
}
