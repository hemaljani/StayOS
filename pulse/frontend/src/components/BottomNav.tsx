// BottomNav - the four-tab PULSE bottom navigation.
//
// Requirement 15.1: exactly four tabs labeled PULSE, VIPs, Ops, and Kitchen
// within the StayOS bottom navigation. PULSE is the default view and lives at
// "/" (Requirement 15.2). Mirrors LUMI's nav styling (fixed, safe-area aware,
// 44px touch targets, active indicator). Hidden on the login page.

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Bell, Star, Wrench, UtensilsCrossed, type LucideIcon } from 'lucide-react';

interface NavItem {
  href: string;
  label: string;
  Icon: LucideIcon;
}

// The four PULSE tabs in display order. PULSE is first and default ("/").
const NAV_ITEMS: NavItem[] = [
  { href: '/', label: 'PULSE', Icon: Bell },
  { href: '/vips/', label: 'VIPs', Icon: Star },
  { href: '/ops/', label: 'Ops', Icon: Wrench },
  { href: '/kitchen/', label: 'Kitchen', Icon: UtensilsCrossed },
];

export default function BottomNav() {
  const pathname = usePathname();

  // Hide the bottom nav on the login page.
  if (pathname.startsWith('/login')) return null;

  return (
    <nav className="fixed bottom-0 left-0 right-0 bg-surface border-t border-gray-800 pb-[var(--sab)] z-50">
      <div className="flex items-center justify-around h-14 max-w-md mx-auto">
        {NAV_ITEMS.map((item) => {
          // The PULSE tab ("/") is active only on an exact match; others match by prefix.
          const isActive =
            item.href === '/'
              ? pathname === '/' || pathname === ''
              : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={isActive ? 'page' : undefined}
              className={`flex flex-col items-center justify-center min-w-[44px] min-h-[44px] px-2 relative ${
                isActive ? 'text-accent' : 'text-gray-500'
              }`}
            >
              <item.Icon size={20} strokeWidth={isActive ? 2.5 : 1.5} />
              <span className="text-[10px] mt-0.5 font-medium">{item.label}</span>
              {isActive && <span className="absolute bottom-0 w-6 h-0.5 bg-accent rounded-full" />}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
