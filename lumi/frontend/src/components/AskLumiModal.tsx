'use client';

import { useCallback, useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Mic, MessageCircle, X } from 'lucide-react';

/**
 * Props for the AskLumiModal component controlling its visibility
 * and providing callbacks for the two interaction modes it launches.
 */
interface AskLumiModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSelectVoice: () => void;
  onSelectChat: () => void;
}

/**
 * Bottom-sheet style modal that lets the GM choose between talking to
 * LUMI (voice) or typing questions (chat).
 *
 * Renders as a React portal so it sits above all other UI (z-[70], same
 * tier as VoiceOverlay/ChatPanel — since this modal is dismissed before
 * either of those open, they never need to stack above it). Provides a
 * semi-transparent backdrop that dismisses the modal on tap, an explicit
 * close button, Escape-to-close, and a focus trap while open.
 *
 * Requirements: 4.2, 4.3, 4.4, 4.6
 */
export default function AskLumiModal({ isOpen, onClose, onSelectVoice, onSelectChat }: AskLumiModalProps) {
  const sheetRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const voiceCardRef = useRef<HTMLButtonElement>(null);

  /**
   * Selects the voice option — notifies the parent then closes the modal.
   */
  const handleSelectVoice = useCallback(() => {
    onSelectVoice();
    onClose();
  }, [onSelectVoice, onClose]);

  /**
   * Selects the chat option — notifies the parent then closes the modal.
   */
  const handleSelectChat = useCallback(() => {
    onSelectChat();
    onClose();
  }, [onSelectChat, onClose]);

  // Focus trap: move focus to the first option card when the modal opens
  useEffect(() => {
    if (isOpen) {
      // Small delay to allow portal to render before focusing
      const timer = setTimeout(() => {
        voiceCardRef.current?.focus();
      }, 50);
      return () => clearTimeout(timer);
    }
  }, [isOpen]);

  // Keyboard handler: Escape to close, Tab cycles within the sheet
  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }

      // Focus trap: Tab cycles between the close button and option cards
      if (event.key === 'Tab') {
        const focusableElements = sheetRef.current?.querySelectorAll(
          'button:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (!focusableElements || focusableElements.length === 0) return;

        const firstEl = focusableElements[0] as HTMLElement;
        const lastEl = focusableElements[focusableElements.length - 1] as HTMLElement;

        if (event.shiftKey && document.activeElement === firstEl) {
          event.preventDefault();
          lastEl.focus();
        } else if (!event.shiftKey && document.activeElement === lastEl) {
          event.preventDefault();
          firstEl.focus();
        }
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  // Don't render anything if closed
  if (!isOpen) return null;

  const modal = (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Ask LUMI"
      className="fixed inset-0 z-[70] flex flex-col justify-end"
      // Tapping the backdrop (anywhere outside the sheet) dismisses the modal
      onClick={onClose}
    >
      {/* Semi-transparent backdrop, separate layer behind the sheet */}
      <div className="absolute inset-0 bg-black/60" aria-hidden="true" />

      {/* Bottom sheet — stopPropagation so taps inside don't bubble to the backdrop */}
      <div
        ref={sheetRef}
        onClick={(event) => event.stopPropagation()}
        className="relative bg-surface rounded-t-3xl border-t border-gray-800 px-6 pt-5 pb-[calc(1.5rem+var(--sab))] w-full max-w-md mx-auto"
        style={{ animation: 'askLumiSlideUp 0.25s ease-out' }}
      >
        {/* Close button — top right */}
        <button
          ref={closeButtonRef}
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute top-3 right-3 min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full text-gray-400 hover:text-ink hover:bg-background/60 focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-surface transition-colors"
        >
          <X size={22} />
        </button>

        {/* Title */}
        <h2 className="text-lg font-semibold text-ink text-center mb-5">Ask LUMI</h2>

        {/* Option cards */}
        <div className="flex flex-col gap-3 mb-2">
          <button
            ref={voiceCardRef}
            type="button"
            onClick={handleSelectVoice}
            aria-label="Voice. Talk to LUMI."
            className="flex items-center gap-4 min-h-[44px] bg-background border border-gray-800 rounded-xl px-4 py-4 text-left hover:border-accent focus:outline-none focus:ring-2 focus:ring-accent transition-colors"
          >
            <div className="w-11 h-11 rounded-full bg-accent/20 flex items-center justify-center shrink-0">
              <Mic size={20} className="text-accent" />
            </div>
            <div>
              <p className="text-sm font-semibold text-ink">Voice</p>
              <p className="text-xs text-gray-400">Talk to LUMI</p>
            </div>
          </button>

          <button
            type="button"
            onClick={handleSelectChat}
            aria-label="Chat. Type your questions."
            className="flex items-center gap-4 min-h-[44px] bg-background border border-gray-800 rounded-xl px-4 py-4 text-left hover:border-accent-secondary focus:outline-none focus:ring-2 focus:ring-accent-secondary transition-colors"
          >
            <div className="w-11 h-11 rounded-full bg-accent-secondary/20 flex items-center justify-center shrink-0">
              <MessageCircle size={20} className="text-accent-secondary" />
            </div>
            <div>
              <p className="text-sm font-semibold text-ink">Chat</p>
              <p className="text-xs text-gray-400">Type your questions</p>
            </div>
          </button>
        </div>

        {/* Keyframe animation for the slide-up entrance — globally-unique name to avoid collisions */}
        <style
          dangerouslySetInnerHTML={{
            __html: `
        @keyframes askLumiSlideUp {
          0% { transform: translateY(100%); opacity: 0; }
          100% { transform: translateY(0); opacity: 1; }
        }
      `,
          }}
        />
      </div>
    </div>
  );

  // Render as portal so it sits above everything including BottomNav
  return createPortal(modal, document.body);
}
