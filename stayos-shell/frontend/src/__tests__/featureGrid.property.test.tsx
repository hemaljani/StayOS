// Property test for the StayOS shell feature launcher.
//
// Invariant: every rendered active feature card is a link whose href points at
// that feature's app on the shared StayOS origin, and specifically LUMI always
// links to /lumi/ and PULSE always to /pulse/. Inactive features never render a
// link (they are "Coming Soon" tiles). This guards the launcher's routing
// contract regardless of how the catalog is ordered or extended.

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import fc from 'fast-check';

import FeatureGrid from '@/components/FeatureGrid';
import { FEATURES } from '@/lib/features';

afterEach(() => cleanup());

describe('FeatureGrid launcher routing (property)', () => {
  it('renders LUMI -> /lumi/ and PULSE -> /pulse/, and only active features as links', () => {
    fc.assert(
      fc.property(fc.constant(null), () => {
        cleanup();
        render(<FeatureGrid onLogout={() => {}} />);

        // Active features expose a link to their href; inactive do not.
        for (const feature of FEATURES) {
          const link = screen.queryByRole('link', { name: feature.name });
          if (feature.active && feature.href) {
            if (!link || link.getAttribute('href') !== feature.href) return false;
          } else if (link) {
            return false;
          }
        }

        // The two shipped features route to their known paths.
        const lumi = screen.getByRole('link', { name: 'LUMI' });
        const pulse = screen.getByRole('link', { name: 'PULSE' });
        return lumi.getAttribute('href') === '/lumi/' && pulse.getAttribute('href') === '/pulse/';
      }),
    );
  });

  it('shows a Coming Soon badge for every inactive feature', () => {
    render(<FeatureGrid onLogout={() => {}} />);
    const inactiveCount = FEATURES.filter((f) => !f.active).length;
    expect(screen.getAllByText('Coming Soon')).toHaveLength(inactiveCount);
    // A basic sanity check that within() is usable for future card-scoped assertions.
    expect(within(document.body).getAllByText('Available')).toHaveLength(
      FEATURES.filter((f) => f.active).length,
    );
  });
});
