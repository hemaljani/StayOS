'use client';

import { useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { FileText, DollarSign, Star, Wrench, Sparkles } from 'lucide-react';
import VoiceOverlay from './VoiceOverlay';
import AskLumiModal from './AskLumiModal';
import ChatPanel from './ChatPanel';

const LEFT_NAV_ITEMS = [
  { href: '/', label: 'Brief', Icon: FileText },
  { href: '/revenue/', label: 'Revenue', Icon: DollarSign },
];

const RIGHT_NAV_ITEMS = [
  { href: '/vips/', label: 'VIPs', Icon: Star },
  { href: '/ops/', label: 'Ops', Icon: Wrench },
];

export default function BottomNav() {
  const pathname = usePathname();
  const [isVoiceOpen, setIsVoiceOpen] = useState(false);
  const [isAskLumiOpen, setIsAskLumiOpen] = useState(false);
  const [isChatOpen, setIsChatOpen] = useState(false);

  // Hide bottom nav on login and settings pages
  if (pathname.startsWith('/login') || pathname.startsWith('/settings')) return null;

  return (
    <>
      <nav className="fixed bottom-0 left-0 right-0 bg-surface border-t border-gray-800 pb-[var(--sab)] z-50">
        <div className="flex items-center justify-around h-14 max-w-md mx-auto relative">
          {/* Left nav items */}
          {LEFT_NAV_ITEMS.map((item) => {
            const isActive = pathname === item.href || (item.href !== '/' && pathname.startsWith(item.href));
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex flex-col items-center justify-center min-w-[44px] min-h-[44px] px-2 relative ${
                  isActive ? 'text-accent' : 'text-gray-500'
                }`}
              >
                <item.Icon size={20} strokeWidth={isActive ? 2.5 : 1.5} />
                <span className="text-[10px] mt-0.5 font-medium">{item.label}</span>
                {/* Active indicator - bottom bar */}
                {isActive && <span className="absolute bottom-0 w-6 h-0.5 bg-accent rounded-full" />}
              </Link>
            );
          })}

          {/* Center "Ask Lumi" button - opens the voice/chat choice modal */}
          <button
            type="button"
            className="flex items-center justify-center w-12 h-12 -mt-5 bg-accent rounded-full shadow-lg shadow-accent/30 text-white active:scale-95 transition-transform"
            aria-label="Ask Lumi"
            onClick={() => setIsAskLumiOpen(true)}
          >
            <Sparkles size={22} strokeWidth={2} />
          </button>

          {/* Right nav items */}
          {RIGHT_NAV_ITEMS.map((item) => {
            const isActive = pathname === item.href || (item.href !== '/' && pathname.startsWith(item.href));
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex flex-col items-center justify-center min-w-[44px] min-h-[44px] px-2 relative ${
                  isActive ? 'text-accent' : 'text-gray-500'
                }`}
              >
                <item.Icon size={20} strokeWidth={isActive ? 2.5 : 1.5} />
                <span className="text-[10px] mt-0.5 font-medium">{item.label}</span>
                {/* Active indicator - bottom bar */}
                {isActive && <span className="absolute bottom-0 w-6 h-0.5 bg-accent rounded-full" />}
              </Link>
            );
          })}
        </div>
      </nav>

      {/* Ask Lumi modal - lets the GM choose between Voice and Chat */}
      <AskLumiModal
        isOpen={isAskLumiOpen}
        onClose={() => setIsAskLumiOpen(false)}
        onSelectVoice={() => setIsVoiceOpen(true)}
        onSelectChat={() => setIsChatOpen(true)}
      />

      {/* Voice assistant overlay - opens over current screen */}
      <VoiceOverlay isOpen={isVoiceOpen} onClose={() => setIsVoiceOpen(false)} />

      {/* Chat panel - text-based conversation with LUMI */}
      <ChatPanel isOpen={isChatOpen} onClose={() => setIsChatOpen(false)} />
    </>
  );
}
