import type { ActionItem } from '@/lib/types';
import ActionItemCard from './ActionItemCard';

interface ActionItemsListProps {
  items: ActionItem[];
  onOverbookingTap?: () => void;
}

export default function ActionItemsList({ items, onOverbookingTap }: ActionItemsListProps) {
  return (
    <div className="space-y-2 mb-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-2">Action Items</h3>
      {items.map((item) => (
        <ActionItemCard
          key={item.id}
          item={item}
          onTap={item.type === 'OVERBOOKING_RISK' ? onOverbookingTap : undefined}
        />
      ))}
    </div>
  );
}
