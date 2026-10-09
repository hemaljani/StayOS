// KpiCard - a single metric tile on the PULSE KPI grid.
//
// Mirrors LUMI's KpiCard shape (surface tile, label, large value, optional
// sublabel) with an optional accent color for the value so alert-severity
// counts read at a glance.

interface KpiCardProps {
  label: string;
  value: string | number;
  sublabel?: string;
  // Tailwind text-color class for the value (e.g. "text-tier-critical").
  valueColor?: string;
  // Keep longer labels readable when their full wording carries meaning.
  wrapLabel?: boolean;
}

export default function KpiCard({ label, value, sublabel, valueColor, wrapLabel = false }: KpiCardProps) {
  return (
    <div className="bg-surface rounded-xl p-3">
      <p className={`text-[10px] uppercase tracking-wide text-gray-400 mb-1 ${wrapLabel ? 'break-words' : 'truncate'}`}>{label}</p>
      <p className={`text-2xl font-bold tabular-nums ${valueColor ?? 'text-ink'}`}>{value}</p>
      {sublabel && <p className="text-[10px] text-gray-500 mt-0.5">{sublabel}</p>}
    </div>
  );
}
