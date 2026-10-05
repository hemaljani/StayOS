// TriageModal - alert detail + Article 14 human-approval gate (Requirement 15.7, 15.8).
//
// Opened when a GM selects an alert card. It fetches the full alert via
// GET /alerts/{alertId} (including the triage brief) and renders:
//   - the situation (tier banner, title, detail);
//   - when a triage brief exists: the confidence score, agent summary, and the
//     ranked options as a single-select list (the recommended option pre-selected);
//   - an approval action control that submits the chosen option to
//     POST /alerts/{alertId}/approvals (decision "approve").
//
// On a successful submission it shows a confirmation indicator reflecting the
// approved state; on failure it shows an error indicator that the approval was
// not recorded and retains the unapproved state (Requirement 15.8). A secondary
// "Reject" control records a rejection (decision "reject") per Requirement 5.7.
//
// Realtime propagation of the resulting RESOLVED status change (AppSync Events)
// is wired in a later batch (Task 21.3); here the parent refetches the feed via
// onActionComplete so the card moves to resolved history.

'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { X, Sparkles, CheckCircle2, AlertCircle, ShieldAlert, CalendarClock } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { useAlertDetail } from '@/hooks/useAlertDetail';
import { TIER_PILL_COLORS, TIER_LABELS } from '@/lib/constants';
import { relativeTime } from '@/lib/format';
import type {
  Alert,
  ApprovalResponse,
  ApprovalState,
  ForecastDetail,
  RankedOption,
} from '@/lib/types';

interface TriageModalProps {
  // The alert selected in the feed (feed copy used for an instant header render).
  alert: Alert;
  onClose: () => void;
  // Called after a successful approve/reject so the parent can refetch the feed.
  onActionComplete: () => void;
}

// Local submission phase for the approval control.
type SubmitPhase = 'idle' | 'submitting' | 'success' | 'error';

export default function TriageModal({ alert, onClose, onActionComplete }: TriageModalProps) {
  // Fetch the authoritative detail (incl. triage brief) for this alert.
  const { alert: detail, loading, error } = useAlertDetail(alert.alertId);

  // Prefer the freshly-fetched detail; fall back to the feed copy while loading.
  const current: Alert = detail ?? alert;
  const brief = current.triageBrief;

  const [selectedOption, setSelectedOption] = useState<string | null>(null);
  const [phase, setPhase] = useState<SubmitPhase>('idle');
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [approvalState, setApprovalState] = useState<ApprovalState>('PENDING');

  // Seed the selected option (recommended first) and existing approval state once
  // the detail arrives.
  useEffect(() => {
    if (!brief) return;
    const recommended = brief.options.find((option) => option.recommended);
    const first = brief.options[0];
    setSelectedOption((prev) => prev ?? recommended?.label ?? first?.label ?? null);
    if (detail?.approval?.state) {
      setApprovalState(detail.approval.state);
    }
  }, [brief, detail]);

  // Close on Escape for keyboard accessibility.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const alreadyDecided = approvalState !== 'PENDING' || current.status === 'RESOLVED';

  // INFO alerts (premium cancellation, VIP check-in) receive an ADVISORY
  // opportunity brief: ranked suggestions the GM acts on manually. They have no
  // executable write-back (INFO is excluded from the backend's RESOLVABLE_TYPES,
  // and the approvals endpoint would reject them), so the human-approval execute
  // control is replaced with an advisory footer. The brief itself still renders.
  //
  // FORECAST_OVERSELL alerts are always advisory too, regardless of tier: a
  // predictive forecast is never executed (the backend sets applied=false), so a
  // WARNING-tier forecast must still stay advisory - never approve/reject.
  const isAdvisory = current.tier === 'INFO' || current.type === 'FORECAST_OVERSELL';

  // Submit an approve/reject decision to the approvals endpoint.
  const submitDecision = useCallback(
    async (decision: 'approve' | 'reject') => {
      if (decision === 'approve' && !selectedOption) return;
      setPhase('submitting');
      setSubmitError(null);
      try {
        const response = await authFetch<ApprovalResponse>(
          `/alerts/${encodeURIComponent(current.alertId)}/approvals`,
          {
            method: 'POST',
            body: JSON.stringify({
              decision,
              selectedOption: decision === 'approve' ? selectedOption : null,
            }),
          }
        );
        if (!response.accepted) {
          // Backend rejected the decision (e.g. already-decided): treat as error
          // and retain the unapproved state (Requirement 15.8).
          setPhase('error');
          setSubmitError(response.reason || 'The decision could not be recorded.');
          return;
        }
        setApprovalState(response.approvalState);
        setPhase('success');
        // Refresh the feed so the resolved card moves to history.
        onActionComplete();
      } catch (err: unknown) {
        setPhase('error');
        setSubmitError(err instanceof Error ? err.message : 'The approval was not recorded.');
      }
    },
    [current.alertId, selectedOption, onActionComplete]
  );

  const approveLabel = useMemo(() => brief?.executeLabel || 'Approve selected option', [brief]);

  return (
    <div
      className="fixed inset-0 bg-black/70 z-[60] flex items-end"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="triage-modal-title"
        className="bg-surface w-full max-w-md mx-auto rounded-t-2xl p-5 max-h-[88vh] overflow-y-auto animate-slide-up"
        onClick={(event) => event.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full ${TIER_PILL_COLORS[current.tier]}`}>
              {TIER_LABELS[current.tier]}
            </span>
            <span className="text-[10px] text-gray-500">{relativeTime(current.createdAt)}</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-gray-400 hover:text-ink p-1 -mt-1 -mr-1"
          >
            <X size={20} />
          </button>
        </div>

        <h2 id="triage-modal-title" className="text-base font-semibold text-ink mb-1">
          {current.title}
        </h2>
        <p className="text-sm text-gray-300 leading-relaxed mb-4">{current.detail}</p>

        {/* Mandatory review chip */}
        {current.escalationStatus === 'MANDATORY_GM_REVIEW' && (
          <div className="flex items-center gap-2 mb-4 text-xs text-tier-warning">
            <ShieldAlert size={14} aria-hidden />
            <span>Flagged for mandatory GM review.</span>
          </div>
        )}

        {/* Predictive FORECAST_OVERSELL advisory. A forecast alert carries its
            AI-authored plan in current.forecast (not a triageBrief), and is never
            executable: it renders the anticipated condition, confidence, the
            oversold count, and the recommendation, followed by the shared
            advisory footer - never approve/reject controls (Requirement 5.3,
            5.4, 4.7). */}
        {current.type === 'FORECAST_OVERSELL' && current.forecast && (
          <ForecastAdvisory forecast={current.forecast} />
        )}

        {/* Detail loading / error */}
        {loading && <p className="text-sm text-gray-400 py-4">Loading triage brief...</p>}
        {error && !detail && (
          <p className="text-sm text-danger py-4">{error}</p>
        )}

        {/* Triage brief */}
        {brief && (
          <section aria-label="Agent triage brief" className="border-t border-gray-800 pt-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="flex items-center gap-1.5 text-sm font-semibold text-ink">
                <Sparkles size={15} className="text-accent" aria-hidden /> Agent Triage
              </h3>
              {/* Confidence score */}
              <span className="text-xs font-semibold text-accent tabular-nums">
                {brief.confidence}% confidence
              </span>
            </div>

            <p className="text-xs text-gray-300 leading-relaxed mb-4">{brief.summary}</p>

            {/* Ranked options - single select */}
            <fieldset className="space-y-2 mb-4" disabled={alreadyDecided || phase === 'submitting'}>
              <legend className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                Ranked options
              </legend>
              {[...brief.options]
                .sort((a, b) => a.rank - b.rank)
                .map((option) => (
                  <OptionRow
                    key={option.label}
                    option={option}
                    checked={selectedOption === option.label}
                    onSelect={() => setSelectedOption(option.label)}
                  />
                ))}
            </fieldset>

            {/* Walk strategy summary (Walk Risk alerts) */}
            {brief.walkStrategy && (
              <div className="mb-4 rounded-lg bg-background border border-gray-800 p-3 text-xs text-gray-300">
                <p className="font-medium text-ink mb-1">Walk strategy</p>
                <p>
                  {brief.walkStrategy.walkableGuests.length > 0
                    ? `${brief.walkStrategy.walkableGuests.length} lowest-eligible guest${
                        brief.walkStrategy.walkableGuests.length === 1 ? '' : 's'
                      } identified to move first; all higher-tier guests protected on property.`
                    : 'No eligible guests to move; absorb the shortfall in-house (upgrades, held-room release, rush-clean).'}
                </p>
                <p className="mt-0.5 text-gray-400">
                  In-house first, then a nearby partner hotel as a last resort ·{' '}
                  {brief.walkStrategy.compensation.length} compensation package
                  {brief.walkStrategy.compensation.length === 1 ? '' : 's'} drafted
                </p>
              </div>
            )}

            {/* Advisory INFO briefs carry no executable action: show a note in
                place of the approve/reject controls. Actionable CRITICAL/WARNING
                briefs show the Article 14 human-approval gate. */}
            {isAdvisory ? (
              <AdvisoryFooter />
            ) : (
              <>
                {/* Approval indicators + controls */}
                <ApprovalControls
                  phase={phase}
                  approvalState={approvalState}
                  alreadyDecided={alreadyDecided}
                  canApprove={selectedOption !== null}
                  approveLabel={approveLabel}
                  submitError={submitError}
                  onApprove={() => submitDecision('approve')}
                  onReject={() => submitDecision('reject')}
                />

                <p className="mt-3 text-[10px] text-gray-500 leading-relaxed">
                  Human approval is required before any action executes (EU AI Act Article 14).
                </p>
              </>
            )}
          </section>
        )}

        {/* No brief available */}
        {!loading && !brief && (
          <p className="text-sm text-gray-400 border-t border-gray-800 pt-4">
            No agent triage brief is available for this alert.
          </p>
        )}
      </div>
    </div>
  );
}

// A single ranked-option row rendered as an accessible radio.
function OptionRow({
  option,
  checked,
  onSelect,
}: {
  option: RankedOption;
  checked: boolean;
  onSelect: () => void;
}) {
  return (
    <label
      className={`flex items-start gap-3 rounded-lg border p-3 cursor-pointer ${
        checked ? 'border-accent bg-accent/5' : 'border-gray-800 bg-background'
      }`}
    >
      <input
        type="radio"
        name="ranked-option"
        className="mt-1 accent-accent"
        checked={checked}
        onChange={onSelect}
      />
      <span className="flex-1 min-w-0">
        <span className="flex items-center gap-2">
          <span className="text-xs font-bold text-gray-300">{option.label}</span>
          <span className="text-sm font-medium text-ink">{option.title}</span>
          {option.recommended && (
            <span className="text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-accent/15 text-accent">
              Recommended
            </span>
          )}
        </span>
        <span className="block text-xs text-gray-400 mt-1">{option.detail}</span>
        {/* Complaint options carry an estimated cost and review-risk level */}
        {(option.estimatedCost !== undefined || option.reviewRisk) && (
          <span className="block text-[10px] text-gray-500 mt-1">
            {option.estimatedCost !== undefined && `Est. cost $${option.estimatedCost}`}
            {option.estimatedCost !== undefined && option.reviewRisk && ' · '}
            {option.reviewRisk && `Review risk: ${option.reviewRisk}`}
          </span>
        )}
      </span>
    </label>
  );
}

// Shared advisory footer: the "no automated action" note shown in place of the
// approve/reject controls for every advisory alert (INFO briefs and
// FORECAST_OVERSELL forecasts). Reused so both paths render identical markup.
function AdvisoryFooter() {
  return (
    <div className="flex items-center gap-2 rounded-lg bg-accent/10 border border-accent/30 px-3 py-2.5 text-xs text-gray-300">
      <Sparkles size={14} className="text-accent" aria-hidden />
      <span>Advisory suggestions - no automated action. Act on these at your discretion.</span>
    </div>
  );
}

// FORECAST_OVERSELL advisory panel. Renders the predictive forecast's
// anticipated condition, a confidence chip, the oversold count, and the
// AI-authored remediation plan (narrative + steps + optional sister-property
// overflow), then the shared advisory footer. A forecast is never executable, so
// no approve/reject controls are rendered here (Requirement 5.3, 5.4, 4.7).
function ForecastAdvisory({ forecast }: { forecast: ForecastDetail }) {
  const { recommendation } = forecast;
  return (
    <section aria-label="Forecast advisory" className="border-t border-gray-800 pt-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-ink">
          <CalendarClock size={15} className="text-accent" aria-hidden /> Predicted Oversell
        </h3>
        {/* Confidence chip - text label + number so it is readable without color. */}
        <span className="text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full bg-accent/15 text-accent tabular-nums">
          {forecast.confidence}% confidence
        </span>
      </div>

      {/* Anticipated condition summary as a description list for semantic markup. */}
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 mb-4 text-xs">
        <div>
          <dt className="text-[10px] uppercase tracking-wide text-gray-500">Anticipated date</dt>
          <dd className="text-gray-200 mt-0.5">{forecast.anticipatedDate}</dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wide text-gray-500">Rooms oversold</dt>
          <dd className="text-gray-200 mt-0.5 tabular-nums">{forecast.roomsOversold}</dd>
        </div>
      </dl>

      {forecast.basedOnPartialData && (
        <div className="flex items-center gap-2 mb-4 text-xs text-tier-warning">
          <AlertCircle size={14} aria-hidden />
          <span>Based on partial data - treat this projection as directional.</span>
        </div>
      )}

      {/* AI-authored remediation plan. */}
      <div className="rounded-lg bg-background border border-gray-800 p-3 mb-4">
        <p className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-gray-500 mb-2">
          <Sparkles size={12} className="text-accent" aria-hidden /> Recommended plan
        </p>
        <p className="text-xs text-gray-300 leading-relaxed">{recommendation.narrative}</p>

        {recommendation.steps.length > 0 && (
          <ol className="list-decimal list-inside space-y-1 mt-3 text-xs text-gray-300">
            {recommendation.steps.map((step, index) => (
              <li key={index} className="leading-relaxed">
                {step}
              </li>
            ))}
          </ol>
        )}

        {/* Sister-property overflow candidate, when the plan carries one. */}
        {recommendation.overflowOption && (
          <p className="mt-3 text-[11px] text-gray-400 leading-relaxed">
            Overflow option: {recommendation.overflowOption.availableRooms} room
            {recommendation.overflowOption.availableRooms === 1 ? '' : 's'} available at sister
            property {recommendation.overflowOption.sisterPropertyId}.
          </p>
        )}
      </div>

      <AdvisoryFooter />
    </section>
  );
}

// The approval action control plus success/error indicators (Requirement 15.8).
function ApprovalControls({
  phase,
  approvalState,
  alreadyDecided,
  canApprove,
  approveLabel,
  submitError,
  onApprove,
  onReject,
}: {
  phase: SubmitPhase;
  approvalState: ApprovalState;
  alreadyDecided: boolean;
  canApprove: boolean;
  approveLabel: string;
  submitError: string | null;
  onApprove: () => void;
  onReject: () => void;
}) {
  // Confirmation indicator reflecting the approved/rejected state.
  if (approvalState === 'APPROVED') {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-success/10 border border-success/30 px-3 py-2.5 text-sm text-success">
        <CheckCircle2 size={16} aria-hidden />
        <span>Approved. The action has been authorized.</span>
      </div>
    );
  }
  if (approvalState === 'REJECTED') {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-gray-500/10 border border-gray-600 px-3 py-2.5 text-sm text-gray-300">
        <AlertCircle size={16} aria-hidden />
        <span>All options rejected. The alert is retained pending further action.</span>
      </div>
    );
  }

  return (
    <div>
      {/* Error indicator: approval was not recorded; unapproved state retained */}
      {phase === 'error' && (
        <div
          role="alert"
          className="flex items-center gap-2 rounded-lg bg-danger/10 border border-danger/30 px-3 py-2.5 text-sm text-danger mb-2"
        >
          <AlertCircle size={16} aria-hidden />
          <span>{submitError || 'The approval was not recorded. Please try again.'}</span>
        </div>
      )}

      <button
        type="button"
        onClick={onApprove}
        disabled={alreadyDecided || phase === 'submitting' || !canApprove}
        className="w-full rounded-lg bg-gradient-to-r from-accent to-accent-secondary py-3 text-sm font-bold text-white shadow-lg shadow-accent/20 disabled:opacity-50 transition-opacity"
      >
        {phase === 'submitting' ? 'Submitting...' : approveLabel}
      </button>

      <button
        type="button"
        onClick={onReject}
        disabled={alreadyDecided || phase === 'submitting'}
        className="w-full mt-2 rounded-lg border border-gray-700 py-2.5 text-xs font-medium text-gray-400 hover:text-ink disabled:opacity-50 transition-colors"
      >
        Reject all options
      </button>
    </div>
  );
}
