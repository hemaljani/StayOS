// UI test for Task 21.7 - agent-ready badge (Requirement 10.5).
//
// An alert card shows the "Agent ready" badge when the alert carries a triage
// brief (hasTriageBrief), and omits it when there is no brief.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import AlertCard from '@/components/AlertCard';
import { makeAlert } from './alertFixtures';

describe('AlertCard agent-ready badge (Requirement 10.5)', () => {
  it('shows the agent-ready badge when the alert has a triage brief', () => {
    const alert = makeAlert({
      tier: 'CRITICAL',
      status: 'UNACKNOWLEDGED',
      triageBrief: { summary: 'Agent triage ready', confidence: 82, options: [] },
    });

    render(<AlertCard alert={alert} onSelect={vi.fn()} />);

    expect(screen.getByText('Agent ready')).toBeInTheDocument();
  });

  it('omits the agent-ready badge when the alert has no triage brief', () => {
    const alert = makeAlert({
      tier: 'CRITICAL',
      status: 'UNACKNOWLEDGED',
      triageBrief: null,
    });

    render(<AlertCard alert={alert} onSelect={vi.fn()} />);

    expect(screen.queryByText('Agent ready')).not.toBeInTheDocument();
  });
});
