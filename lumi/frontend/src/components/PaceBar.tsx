interface PaceBarProps {
  label: string;
  percentage: number;
  value?: string;
}

export default function PaceBar({ label, percentage, value }: PaceBarProps) {
  const clampedPct = Math.min(Math.max(percentage, 0), 120);
  const barWidth = Math.min(clampedPct, 100);

  // Color coding: green >= 100%, warning 80-99%, danger < 80%
  const barColor =
    percentage >= 100
      ? 'bg-success'
      : percentage >= 80
        ? 'bg-warning'
        : 'bg-danger';

  const textColor =
    percentage >= 100
      ? 'text-success'
      : percentage >= 80
        ? 'text-warning'
        : 'text-danger';

  return (
    <div className="mb-3">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-gray-400">{label}</span>
        <span className={`text-xs font-semibold ${textColor}`}>
          {percentage}%{value ? ` (${value})` : ''}
        </span>
      </div>
      <div className="h-2 bg-surface rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${barColor}`}
          style={{ width: `${barWidth}%` }}
        />
      </div>
    </div>
  );
}
