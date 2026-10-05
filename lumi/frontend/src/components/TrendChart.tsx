'use client';

import type { BriefHistorySummary } from '@/lib/types';

interface TrendChartProps {
  data: BriefHistorySummary[];
}

// Chart layout constants (SVG viewBox units)
const VIEW_WIDTH = 350;
const VIEW_HEIGHT = 180;
const PAD_TOP = 20;
const PAD_RIGHT = 20;
const PAD_BOTTOM = 30;
const PAD_LEFT = 40;
const PLOT_WIDTH = VIEW_WIDTH - PAD_LEFT - PAD_RIGHT;   // 290
const PLOT_HEIGHT = VIEW_HEIGHT - PAD_TOP - PAD_BOTTOM; // 130

// Metric line colors
const COLORS = {
  occupancy: '#6b8fff',
  adr: '#2dd4a0',
  revPAR: '#f5a623',
} as const;

/**
 * Maps an array of numeric values to SVG Y coordinates with auto-scaling.
 * Returns an array of { x, y } points for the polyline.
 */
function calculatePoints(values: number[], count: number): { x: number; y: number }[] {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min;

  return values.map((val, i) => {
    const x = PAD_LEFT + (count > 1 ? (i * PLOT_WIDTH) / (count - 1) : PLOT_WIDTH / 2);
    // If all values are identical, center the line vertically
    const normalised = range === 0 ? 0.5 : (val - min) / range;
    const y = PAD_TOP + PLOT_HEIGHT - normalised * PLOT_HEIGHT;
    return { x, y };
  });
}

function pointsToPolyline(points: { x: number; y: number }[]): string {
  return points.map((p) => `${p.x},${p.y}`).join(' ');
}

export default function TrendChart({ data }: TrendChartProps) {
  // Fallback when insufficient data
  if (data.length < 2) {
    return (
      <p className="text-sm text-gray-500 text-center py-6">
        Not enough data for trends
      </p>
    );
  }

  const count = data.length;

  // Extract metric arrays
  const occValues = data.map((d) => d.dailyKPIs.occupancy.current);
  const adrValues = data.map((d) => d.dailyKPIs.adr.current);
  const revParValues = data.map((d) => d.dailyKPIs.revPAR.current);

  // Calculate SVG coordinates for each line
  const occPoints = calculatePoints(occValues, count);
  const adrPoints = calculatePoints(adrValues, count);
  const revParPoints = calculatePoints(revParValues, count);

  // X-axis labels: abbreviated weekday from briefDate
  const dayLabels = data.map((d) =>
    new Date(d.briefDate + 'T00:00:00').toLocaleDateString('en-US', { weekday: 'short' })
  );

  // Today's values (last entry)
  const today = data[count - 1];

  return (
    <div>
      <svg
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        width="100%"
        preserveAspectRatio="xMidYMid meet"
        className="overflow-visible"
      >
        {/* Drop-shadow filter for today's points */}
        <defs>
          <filter id="glow">
            <feDropShadow dx="0" dy="0" stdDeviation="2" floodOpacity="0.6" />
          </filter>
        </defs>

        {/* Occupancy line */}
        <polyline
          points={pointsToPolyline(occPoints)}
          fill="none"
          stroke={COLORS.occupancy}
          strokeWidth={2}
        />
        {occPoints.map((p, i) => (
          <circle
            key={`occ-${i}`}
            cx={p.x}
            cy={p.y}
            r={i === count - 1 ? 5 : 3}
            fill={COLORS.occupancy}
            filter={i === count - 1 ? 'url(#glow)' : undefined}
          />
        ))}

        {/* ADR line */}
        <polyline
          points={pointsToPolyline(adrPoints)}
          fill="none"
          stroke={COLORS.adr}
          strokeWidth={2}
        />
        {adrPoints.map((p, i) => (
          <circle
            key={`adr-${i}`}
            cx={p.x}
            cy={p.y}
            r={i === count - 1 ? 5 : 3}
            fill={COLORS.adr}
            filter={i === count - 1 ? 'url(#glow)' : undefined}
          />
        ))}

        {/* RevPAR line */}
        <polyline
          points={pointsToPolyline(revParPoints)}
          fill="none"
          stroke={COLORS.revPAR}
          strokeWidth={2}
        />
        {revParPoints.map((p, i) => (
          <circle
            key={`rev-${i}`}
            cx={p.x}
            cy={p.y}
            r={i === count - 1 ? 5 : 3}
            fill={COLORS.revPAR}
            filter={i === count - 1 ? 'url(#glow)' : undefined}
          />
        ))}

        {/* X-axis weekday labels */}
        {dayLabels.map((label, i) => {
          const x = PAD_LEFT + (count > 1 ? (i * PLOT_WIDTH) / (count - 1) : PLOT_WIDTH / 2);
          return (
            <text
              key={`label-${i}`}
              x={x}
              y={VIEW_HEIGHT - 5}
              textAnchor="middle"
              className="fill-gray-500"
              fontSize={10}
            >
              {label}
            </text>
          );
        })}
      </svg>

      {/* Legend: colored dots + metric name + today's value */}
      <div className="flex items-center justify-center gap-4 mt-2 text-xs text-gray-300">
        <span className="flex items-center gap-1">
          <span
            className="inline-block w-2 h-2 rounded-full"
            style={{ backgroundColor: COLORS.occupancy }}
          />
          Occ {today.dailyKPIs.occupancy.current}%
        </span>
        <span className="flex items-center gap-1">
          <span
            className="inline-block w-2 h-2 rounded-full"
            style={{ backgroundColor: COLORS.adr }}
          />
          ADR ${today.dailyKPIs.adr.current}
        </span>
        <span className="flex items-center gap-1">
          <span
            className="inline-block w-2 h-2 rounded-full"
            style={{ backgroundColor: COLORS.revPAR }}
          />
          RevPAR ${today.dailyKPIs.revPAR.current}
        </span>
      </div>
    </div>
  );
}
