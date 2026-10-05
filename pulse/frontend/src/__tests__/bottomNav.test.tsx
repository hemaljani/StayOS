// UI test for Task 21.7 - four-tab structure (Requirement 15.1, 15.2).
//
// BottomNav must render exactly the four StayOS tabs (PULSE, VIPs, Ops, Kitchen)
// in order, and PULSE must be the default route ("/") marked active when the
// current path is "/".

import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';

// usePathname drives the active-tab logic; pin it to the default PULSE route.
vi.mock('next/navigation', () => ({
  usePathname: () => '/',
}));

import BottomNav from '@/components/BottomNav';

describe('BottomNav four-tab structure (Requirement 15.1, 15.2)', () => {
  it('renders exactly PULSE, VIPs, Ops, and Kitchen in order', () => {
    render(<BottomNav />);
    const nav = screen.getByRole('navigation');
    const links = within(nav).getAllByRole('link');

    expect(links).toHaveLength(4);
    expect(links.map((link) => link.textContent)).toEqual(['PULSE', 'VIPs', 'Ops', 'Kitchen']);
  });

  it('makes PULSE the default route and marks it active at "/"', () => {
    render(<BottomNav />);
    const pulseLink = screen.getByText('PULSE').closest('a');

    expect(pulseLink).toHaveAttribute('href', '/');
    expect(pulseLink).toHaveAttribute('aria-current', 'page');

    // No other tab is active when on the PULSE route.
    expect(screen.getByText('VIPs').closest('a')).not.toHaveAttribute('aria-current');
    expect(screen.getByText('Ops').closest('a')).not.toHaveAttribute('aria-current');
    expect(screen.getByText('Kitchen').closest('a')).not.toHaveAttribute('aria-current');
  });
});
