// Account-menu behavior for the shell launcher (FeatureGrid).
//
// The signed-in identity and Logout action live behind a gear icon in the
// top-right (mirroring LUMI's settings affordance). These tests assert the
// menu is closed by default, opens on click to reveal the full name and a
// Logout control, and that Logout fires the callback.

import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

import FeatureGrid from '@/components/FeatureGrid';

afterEach(() => cleanup());

describe('FeatureGrid account menu', () => {
  it('hides the identity and Logout until the gear icon is clicked', () => {
    render(
      <FeatureGrid email="jsmith@aloha.com" name="Jennifer Smith" onLogout={() => {}} />,
    );

    // Collapsed by default: no name, no Logout item on screen.
    expect(screen.queryByText('Jennifer Smith')).toBeNull();
    expect(screen.queryByRole('menuitem', { name: /logout/i })).toBeNull();

    // The gear toggle is present and marked closed.
    const toggle = screen.getByRole('button', { name: /account menu/i });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
  });

  it('opens on click and shows the full name plus a Logout action', () => {
    render(
      <FeatureGrid email="jsmith@aloha.com" name="Jennifer Smith" onLogout={() => {}} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /account menu/i }));

    // Full name is preferred over email; email shown as the secondary line.
    expect(screen.getByText('Jennifer Smith')).toBeTruthy();
    expect(screen.getByText('jsmith@aloha.com')).toBeTruthy();
    expect(screen.getByRole('menuitem', { name: /logout/i })).toBeTruthy();
    // The property brand is shown inside the menu (the centered header no longer
    // duplicates it, so exactly one match once the menu is open).
    expect(screen.getByText('Aloha Hotels & Resorts')).toBeTruthy();
  });

  it('falls back to the email when no name is present', () => {
    render(<FeatureGrid email="jsmith@aloha.com" onLogout={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: /account menu/i }));
    expect(screen.getByText('jsmith@aloha.com')).toBeTruthy();
  });

  it('fires onLogout when the Logout item is clicked', () => {
    const onLogout = vi.fn();
    render(
      <FeatureGrid email="jsmith@aloha.com" name="Jennifer Smith" onLogout={onLogout} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /account menu/i }));
    fireEvent.click(screen.getByRole('menuitem', { name: /logout/i }));
    expect(onLogout).toHaveBeenCalledOnce();
  });
});
