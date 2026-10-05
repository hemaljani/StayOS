interface KpiCardProps {
  label: string;
  value: string | number;
  unit?: string;
  delta?: number;
  deltaLabel?: string;
  sublabel?: string;
}

export default function KpiCard({ label, value, unit, delta, deltaLabel, sublabel }: KpiCardProps) {
  const deltaColor = delta !== undefined ? (delta >= 0 ? 'text-success' : 'text-danger') : '';
  const deltaSign = delta !== undefined ? (delta >= 0 ? '+' : '') : '';

  return (
    <div className="bg-surface rounded-xl p-3">
      <p className="text-xs text-gray-400 mb-1 truncate">{label}</p>
      <p className="text-2xl font-bold">
        {unit === '$' && '$'}{value}{unit === '%' && '%'}
      </p>
      {delta !== undefined && (
        <p className={`text-xs ${deltaColor} mt-0.5`}>
          {deltaSign}{delta}{deltaLabel || ''}
        </p>
      )}
      {sublabel && <p className="text-[10px] text-gray-500 mt-0.5">{sublabel}</p>}
    </div>
  );
}
