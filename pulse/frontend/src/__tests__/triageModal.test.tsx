// UI tests for Task 21.7 - approval success/failure (Requirement 15.8).
//
// TriageModal submits the selected option to POST /alerts/{id}/approvals. On a
// successful response it shows the confirmation indicator; on a failed request it
// shows the error indicator and retains the unapproved state (the approve control
// remains available). Both the detail fetch and the approval POST go through
// authFetch, which is mocked so no network is hit.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { authFetch } from '@/lib/api';
import { makeAlert } from './alertFixtures';
import type { Alert } from '@/lib/types';

// Mock the authenticated fetch wrapper (both the detail GET and approval POST).
vi.mock('@/lib/api', () => ({
  authFetch: vi.fn(),
}));

import TriageModal from '@/components/TriageModal';

const mockedAuthFetch = vi.mocked(authFetch);

// A feed alert with a triage brief carrying one recommended ranked option.
function briefedAlert(): Alert {
  return makeAlert({
    alertId: 'brief-1',
    tier: 'CRITICAL',
    status: 'UNACKNOWLEDGED',
    title: 'Complaint escalation',
    triageBrief: {
      summary: 'Guest complaint requires a service-recovery decision.',
      confidence: 76,
      options: [
        {
          label: 'A',
          rank: 1,
          title: 'Offer a suite upgrade',
          detail: 'Move the guest to a suite and comp one night.',
          recommended: true,
        },
      ],
    },
  });
}

beforeEach(() => {
  mockedAuthFetch.mockReset();
});

describe('TriageModal approval outcomes (Requirement 15.8)', () => {
  it('shows the confirmation indicator on a successful approval', async () => {
    const alert = briefedAlert();
    mockedAuthFetch.mockImplementation(async (path: string) => {
      if (path.includes('/approvals')) {
        return { accepted: true, approvalState: 'APPROVED', executed: true } as never;
      }
      // Detail fetch (GET /alerts/{id}).
      return { alert } as never;
    });

    const onActionComplete = vi.fn();
    render(<TriageModal alert={alert} onClose={vi.fn()} onActionComplete={onActionComplete} />);

    // Wait for the brief to load (ranked option visible), then approve.
    await screen.findByText('Offer a suite upgrade');
    fireEvent.click(screen.getByRole('button', { name: /Approve selected option/ }));

    // Confirmation indicator appears and the feed refetch is triggered.
    await screen.findByText('Approved. The action has been authorized.');
    expect(onActionComplete).toHaveBeenCalledTimes(1);
  });

  it('shows the error indicator and retains the unapproved state on a failed approval', async () => {
    const alert = briefedAlert();
    mockedAuthFetch.mockImplementation(async (path: string) => {
      if (path.includes('/approvals')) {
        throw new Error('The approval was not recorded.');
      }
      return { alert } as never;
    });

    const onActionComplete = vi.fn();
    render(<TriageModal alert={alert} onClose={vi.fn()} onActionComplete={onActionComplete} />);

    await screen.findByText('Offer a suite upgrade');
    fireEvent.click(screen.getByRole('button', { name: /Approve selected option/ }));

    // Error indicator is shown...
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('The approval was not recorded.');
    });
    // ...the unapproved state is retained (no confirmation, approve control still present)...
    expect(screen.queryByText('Approved. The action has been authorized.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Approve selected option/ })).toBeInTheDocument();
    // ...and no feed refetch happened because nothing was approved.
    expect(onActionComplete).not.toHaveBeenCalled();
  });
});


// An INFO alert carrying an advisory opportunity brief (premium cancellation /
// VIP check-in). INFO briefs have no executable write-back, so the modal should
// render the brief but replace the approve/reject controls with an advisory note.
function advisoryInfoAlert(): Alert {
  return makeAlert({
    alertId: 'info-brief-1',
    tier: 'INFO',
    type: 'PREMIUM_CANCELLATION',
    status: 'UNACKNOWLEDGED',
    title: 'Premium cancellation',
    triageBrief: {
      summary: 'Premium room freed by a cancellation; resell before arrival.',
      confidence: 72,
      options: [
        {
          label: 'A',
          rank: 1,
          title: 'Re-list at current rate',
          detail: 'Pace is healthy; re-list across OTA + direct.',
          recommended: true,
        },
        {
          label: 'B',
          rank: 2,
          title: 'Offer as a paid upgrade',
          detail: 'Upsell a confirmed lower-tier guest.',
          recommended: false,
        },
      ],
      executeLabel: 'Re-list premium room',
    },
  });
}

describe('TriageModal advisory INFO briefs', () => {
  it('renders the brief but shows an advisory note instead of an approve control', async () => {
    const alert = advisoryInfoAlert();
    mockedAuthFetch.mockImplementation(async () => ({ alert }) as never);

    render(<TriageModal alert={alert} onClose={vi.fn()} onActionComplete={vi.fn()} />);

    // The advisory brief content (summary + ranked options) still renders.
    await screen.findByText('Re-list at current rate');
    expect(
      screen.getByText('Premium room freed by a cancellation; resell before arrival.')
    ).toBeInTheDocument();
    expect(screen.getByText('72% confidence')).toBeInTheDocument();

    // The advisory footer replaces the approval controls.
    expect(
      screen.getByText(/Advisory suggestions - no automated action/)
    ).toBeInTheDocument();

    // No executable approve/reject controls are offered for an INFO alert, and
    // the Article 14 human-approval note is not shown.
    expect(
      screen.queryByRole('button', { name: /Re-list premium room/ })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Approve selected option/ })
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Reject all options/)).not.toBeInTheDocument();
    expect(screen.queryByText(/EU AI Act Article 14/)).not.toBeInTheDocument();
  });
});

// A WARNING-tier FORECAST_OVERSELL alert carrying a predictive forecast payload
// (not a triageBrief). Forecast alerts are always advisory regardless of tier -
// a WARNING forecast must still render the advisory footer and never expose
// approve/reject controls (Requirement 5.4). The forecast object matches the
// ForecastDetail type in types.ts (flat recommendation shape).
function forecastOversellAlert(): Alert {
  return makeAlert({
    alertId: 'forecast-1',
    tier: 'WARNING',
    type: 'FORECAST_OVERSELL',
    status: 'UNACKNOWLEDGED',
    title: 'Predicted oversell',
    forecast: {
      conditionType: 'OVERSELL',
      anticipatedDate: '2025-07-14',
      confidence: 88,
      roomsOversold: 4,
      basedOnPartialData: false,
      recommendation: {
        narrative: 'Demand is pacing ahead of supply; rebalance before arrival day.',
        steps: ['Tighten remaining OTA inventory.', 'Pre-arrange sister-property overflow.'],
        roomsOversold: 4,
        overflowOption: { sisterPropertyId: 'ALOHA-MIA-001', availableRooms: 6 },
      },
    },
  });
}

describe('TriageModal WARNING forecast advisory', () => {
  it('renders the forecast advisory but shows no approve/reject controls (Requirement 5.4)', async () => {
    const alert = forecastOversellAlert();
    // Detail fetch (GET /alerts/{id}) returns the forecast alert so the modal has
    // the forecast payload to render its advisory branch.
    mockedAuthFetch.mockImplementation(async () => ({ alert }) as never);

    render(<TriageModal alert={alert} onClose={vi.fn()} onActionComplete={vi.fn()} />);

    // The forecast advisory content renders: narrative, confidence chip, rooms
    // oversold, and the anticipated date.
    await screen.findByText('Demand is pacing ahead of supply; rebalance before arrival day.');
    expect(screen.getByText('88% confidence')).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('2025-07-14')).toBeInTheDocument();

    // The advisory footer renders (same note as the INFO advisory path).
    expect(
      screen.getByText(/Advisory suggestions - no automated action/)
    ).toBeInTheDocument();

    // Despite the WARNING tier, a forecast stays advisory: no executable
    // approve/reject controls and no Article 14 human-approval note.
    expect(
      screen.queryByRole('button', { name: /Approve selected option/ })
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Reject all options/)).not.toBeInTheDocument();
    expect(screen.queryByText(/EU AI Act Article 14/)).not.toBeInTheDocument();
  });
});
