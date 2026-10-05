// AlertCard - a single alert row in the PULSE live feed or resolved history.
//
// Shows the tier pill, relative time, title, and detail. When the alert has an
// associated triage brief it displays an agent-ready badge (Requirement 10.5),
// and when it is flagged for mandatory GM review it shows an escalation chip.
// Selecting a card opens the triage modal (Requirement 15.7). Resolved cards are
// visually dimmed and show who resolved them.

import { Sparkles, ChevronRight, CheckCircle2, ShieldAlert, UserCheck } from 'lucide-react';
import type { Alert } from '@/lib/types';
import { TIER_PILL_COLORS, TIER_BORDER_COLORS, TIER_LABELS } from '@/lib/constants';
import { relativeTime, hasTriageBrief } from '@/lib/format';

interface AlertCardProps {
  alert: Alert;
  onSelect: (alert: Alert) => void;
  // When true, this alert was escalation-assigned to the current GM via the
  // per-user realtime channel; it gets a distinct "assigned to you" treatment
  // (Task 21.3, design Component 6).
  assigned?: boolean;
}

export default function AlertCard({ alert, onSelect, assigned = false }: AlertCardProps) {
  const isResolved = alert.status === 'RESOLVED';
  const agentReady = hasTriageBrief(alert);
  const mandatoryReview = alert.escalationStatus === 'MANDATORY_GM_REVIEW';
  // Highlight assigned (escalated-to-you) live cards with an accent ring.
  const assignedHighlight = assigned && !isResolved;

  return (
    <button
      type="button"
      onClick={() => onSelect(alert)}
      className={`w-full text-left bg-surface rounded-xl border border-gray-800 border-l-[3px] ${
        TIER_BORDER_COLORS[alert.tier]
      } p-3 active:scale-[0.99] transition-transform ${isResolved ? 'opacity-60' : ''} ${
        assignedHighlight ? 'ring-1 ring-accent/60 shadow-lg shadow-accent/10' : ''
      }`}
    >
      {/* Assigned-to-you banner (per-user escalation nudge) */}
      {assignedHighlight && (
        <span className="flex items-center gap-1 text-[10px] font-bold text-accent mb-1.5">
          <UserCheck size={12} aria-hidden /> Assigned to you
        </span>
      )}
      {/* Tier + time row */}
      <div className="flex items-center gap-2 mb-1">
        {isResolved ? (
          <span className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full bg-success/15 text-success">
            <CheckCircle2 size={11} aria-hidden /> Resolved
          </span>
        ) : (
          <span className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full ${TIER_PILL_COLORS[alert.tier]}`}>
            {TIER_LABELS[alert.tier]}
          </span>
        )}
        <span className="text-[10px] text-gray-500">
          {relativeTime(isResolved ? alert.resolvedAt ?? alert.createdAt : alert.createdAt)}
        </span>
        {!isResolved && <ChevronRight size={14} className="ml-auto text-gray-600" aria-hidden />}
      </div>

      {/* Title */}
      <p className={`text-sm font-semibold leading-snug ${isResolved ? 'text-gray-400' : 'text-ink'}`}>
        {alert.title}
      </p>

      {/* Detail */}
      <p className="text-xs text-gray-400 mt-1 line-clamp-2">{alert.detail}</p>

      {/* Footer: agent-ready badge + escalation chip (live cards only) */}
      {!isResolved && (agentReady || mandatoryReview) && (
        <div className="flex items-center gap-2 mt-2">
          {agentReady && (
            <span className="inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded-full border border-accent/30 bg-accent/10 text-accent">
              <Sparkles size={11} aria-hidden /> Agent ready
            </span>
          )}
          {mandatoryReview && (
            <span className="inline-flex items-center gap-1 text-[10px] font-medium text-gray-400">
              <ShieldAlert size={11} aria-hidden /> GM review
            </span>
          )}
        </div>
      )}

      {/* Resolved attribution */}
      {isResolved && alert.resolvedBy && (
        <p className="text-[10px] text-gray-500 mt-1.5">Resolved by {alert.resolvedBy}</p>
      )}
    </button>
  );
}
