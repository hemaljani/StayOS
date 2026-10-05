import { SEVERITY_COLORS, SEVERITY_BG_COLORS } from '@/lib/constants';
import type { ActionItem } from '@/lib/types';

interface ActionItemCardProps {
  item: ActionItem;
  onTap?: () => void;
}

export default function ActionItemCard({ item, onTap }: ActionItemCardProps) {
  const severityColor = SEVERITY_COLORS[item.severity];
  const severityBg = SEVERITY_BG_COLORS[item.severity];

  return (
    <button
      onClick={onTap}
      className="w-full bg-surface rounded-xl p-3 flex items-start gap-3 text-left"
    >
      <span className={`text-xs font-bold px-2 py-0.5 rounded ${severityBg} ${severityColor} shrink-0`}>
        {item.severity}
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-ink truncate">{item.title}</p>
        <p className="text-xs text-gray-400 mt-0.5 line-clamp-2">{item.detail}</p>
      </div>
    </button>
  );
}
