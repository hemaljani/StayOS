'use client';

import { useCallback, useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Mic, X, Loader2, AlertCircle } from 'lucide-react';
import { useVoiceAgent } from '@/hooks/useVoiceAgent';
import type { VoiceAgentStatus } from '@/hooks/useVoiceAgent';

/**
 * Props for the VoiceOverlay component controlling its visibility
 * and providing a close callback.
 */
interface VoiceOverlayProps {
  isOpen: boolean;
  onClose: () => void;
}

/**
 * Full-viewport modal overlay for the StayOS voice assistant.
 *
 * Renders as a React portal to ensure it sits above all other UI
 * (z-[70], above BottomNav at z-50). Provides visual state indicators
 * for each phase of the voice interaction lifecycle, real-time transcript
 * display, keyboard accessibility (Escape to close, Enter/Space to toggle),
 * and focus trap behavior while open.
 *
 * Requirements: 7.1, 7.2, 7.3, 7.5, 7.6, 7.7, 7.8
 */
export default function VoiceOverlay({ isOpen, onClose }: VoiceOverlayProps) {
  const { state, startSession, endSession, isConnected } = useVoiceAgent();
  const overlayRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const micButtonRef = useRef<HTMLButtonElement>(null);

  /**
   * Handles closing the overlay and ending any active session.
   */
  const handleClose = useCallback(() => {
    if (isConnected) {
      endSession();
    }
    onClose();
  }, [isConnected, endSession, onClose]);

  /**
   * Toggles the voice session — starts if idle/error, ends if active.
   */
  const toggleSession = useCallback(async () => {
    if (state.status === 'idle' || state.status === 'error') {
      await startSession();
    } else {
      endSession();
    }
  }, [state.status, startSession, endSession]);

  // Focus trap: move focus into the overlay when it opens
  useEffect(() => {
    if (isOpen) {
      // Small delay to allow portal to render before focusing
      const timer = setTimeout(() => {
        micButtonRef.current?.focus();
      }, 50);
      return () => clearTimeout(timer);
    }
  }, [isOpen]);

  // Keyboard handler: Escape to close, Enter/Space to toggle session
  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        handleClose();
        return;
      }

      // Enter/Space toggles session when focus is on the overlay (not on close button)
      if (event.key === 'Enter' || event.key === ' ') {
        const target = event.target as HTMLElement;
        // Only toggle if focused on the mic button or the overlay container itself
        if (target === micButtonRef.current || target === overlayRef.current) {
          event.preventDefault();
          toggleSession();
        }
      }

      // Focus trap: Tab cycles between mic button and close button
      if (event.key === 'Tab') {
        const focusableElements = overlayRef.current?.querySelectorAll(
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
  }, [isOpen, handleClose, toggleSession]);

  // Don't render anything if closed
  if (!isOpen) return null;

  const overlay = (
    <div
      ref={overlayRef}
      role="dialog"
      aria-modal="true"
      aria-label="LUMI Voice Assistant"
      className="fixed inset-0 z-[70] flex flex-col items-center justify-center bg-black/80 backdrop-blur-sm"
    >
      {/* Close / Stop button — top right */}
      <button
        ref={closeButtonRef}
        type="button"
        onClick={handleClose}
        aria-label={isConnected ? 'Stop session and close' : 'Close voice assistant'}
        className="absolute top-4 right-4 min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full bg-surface/80 text-gray-400 hover:text-ink hover:bg-surface focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background transition-colors"
      >
        <X size={22} />
      </button>

      {/* Main content area */}
      <div className="flex flex-col items-center justify-center flex-1 w-full max-w-md px-6 min-w-[390px]">
        {/* Agent name */}
        <h2 className="text-lg font-semibold text-white mb-6">LUMI</h2>

        {/* Transcript area (above mic button) */}
        <div
          aria-live="polite"
          aria-atomic="false"
          className="w-full max-h-[30vh] overflow-y-auto space-y-4 px-2 mb-6"
        >
          {/* User transcript */}
          {state.userTranscript && (
            <div className="text-right">
              <p className="inline-block bg-accent/10 text-gray-200 text-sm rounded-2xl rounded-br-sm px-4 py-2 max-w-[85%]">
                {state.userTranscript}
              </p>
            </div>
          )}

          {/* Agent transcript */}
          {state.agentTranscript && (
            <div className="text-left">
              <p className="inline-block bg-surface text-gray-200 text-sm rounded-2xl rounded-bl-sm px-4 py-2 max-w-[85%]">
                {state.agentTranscript}
              </p>
            </div>
          )}
        </div>

        {/* Visual state indicator (mic button) */}
        <div className="flex flex-col items-center justify-center mb-8">
          <StatusIndicator status={state.status} onTap={toggleSession} micButtonRef={micButtonRef} />
        </div>

        {/* Suggested questions (always visible during session) */}
        {state.status !== 'error' && (
          <div className="w-full space-y-2 mb-6">
            <p className="text-xs text-gray-500 text-center uppercase tracking-wide mb-3">Try asking</p>
            <div className="flex flex-wrap gap-2 justify-center">
              {[
                "What's my occupancy today?",
                "Any VIP arrivals?",
                "How's revenue trending?",
                "Rooms out of order?",
                "Open work orders?",
              ].map((q) => (
                <span
                  key={q}
                  className="text-xs bg-surface/80 text-gray-300 px-3 py-1.5 rounded-full border border-gray-700"
                >
                  {q}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Error display */}
      {state.status === 'error' && state.error && (
        <div className="absolute bottom-24 left-6 right-6 max-w-md mx-auto bg-danger/10 border border-danger/30 rounded-xl px-4 py-3 flex items-start gap-3">
          <AlertCircle size={18} className="text-danger mt-0.5 shrink-0" />
          <p className="text-sm text-gray-300">{state.error}</p>
        </div>
      )}
    </div>
  );

  // Render as portal so it sits above everything including BottomNav
  return createPortal(overlay, document.body);
}

/**
 * Props for the StatusIndicator subcomponent that renders the visual
 * state (mic icon, pulse animation, spinner, audio bars) based on
 * the current voice agent status.
 */
interface StatusIndicatorProps {
  status: VoiceAgentStatus;
  onTap: () => void;
  micButtonRef: React.RefObject<HTMLButtonElement | null>;
}

/**
 * Visual status indicator showing different animations/icons for each
 * voice session state. Acts as the primary interaction target (tap to
 * start/stop session).
 */
function StatusIndicator({ status, onTap, micButtonRef }: StatusIndicatorProps) {
  switch (status) {
    case 'idle':
      return (
        <button
          ref={micButtonRef}
          type="button"
          onClick={onTap}
          aria-label="Tap to speak"
          className="flex flex-col items-center gap-4 min-w-[44px] min-h-[44px] focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background rounded-full p-2 transition-transform active:scale-95"
        >
          <div className="w-20 h-20 rounded-full bg-accent/20 flex items-center justify-center">
            <Mic size={32} className="text-accent" />
          </div>
          <span className="text-sm text-gray-400">Tap to speak</span>
        </button>
      );

    case 'connecting':
      return (
        <div className="flex flex-col items-center gap-4">
          <div className="w-20 h-20 rounded-full bg-accent/10 flex items-center justify-center">
            <Loader2 size={32} className="text-accent animate-spin" />
          </div>
          <span className="text-sm text-gray-400">Connecting...</span>
          {/* Hidden button to maintain ref for focus trap */}
          <button
            ref={micButtonRef}
            type="button"
            disabled
            className="sr-only"
            aria-label="Connecting"
          >
            Connecting
          </button>
        </div>
      );

    case 'listening':
      return (
        <button
          ref={micButtonRef}
          type="button"
          onClick={onTap}
          aria-label="Listening. Tap to stop."
          className="flex flex-col items-center gap-4 min-w-[44px] min-h-[44px] focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background rounded-full p-2 transition-transform active:scale-95"
        >
          {/* Pulse animation rings around mic icon */}
          <div className="relative w-20 h-20 flex items-center justify-center">
            <span className="absolute inset-0 rounded-full bg-accent/20 animate-ping" />
            <span className="absolute inset-2 rounded-full bg-accent/30 animate-pulse" />
            <div className="relative w-16 h-16 rounded-full bg-accent flex items-center justify-center">
              <Mic size={28} className="text-white" />
            </div>
          </div>
          <span className="text-sm text-accent">Listening...</span>
        </button>
      );

    case 'processing':
      return (
        <div className="flex flex-col items-center gap-4">
          <div className="w-20 h-20 rounded-full bg-accent/10 flex items-center justify-center">
            <Loader2 size={32} className="text-accent animate-spin" />
          </div>
          <span className="text-sm text-gray-400">Thinking...</span>
          {/* Hidden button for focus trap */}
          <button
            ref={micButtonRef}
            type="button"
            onClick={onTap}
            aria-label="Processing. Tap to stop."
            className="sr-only"
          >
            Stop
          </button>
        </div>
      );

    case 'speaking':
      return (
        <button
          ref={micButtonRef}
          type="button"
          onClick={onTap}
          aria-label="Agent is speaking. Tap to stop."
          className="flex flex-col items-center gap-4 min-w-[44px] min-h-[44px] focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background rounded-full p-2 transition-transform active:scale-95"
        >
          {/* Audio bars animation */}
          <div className="w-20 h-20 rounded-full bg-accent-secondary/10 flex items-center justify-center">
            <AudioBars />
          </div>
          <span className="text-sm text-accent-secondary">Speaking...</span>
        </button>
      );

    case 'error':
      return (
        <button
          ref={micButtonRef}
          type="button"
          onClick={onTap}
          aria-label="Error occurred. Tap to retry."
          className="flex flex-col items-center gap-4 min-w-[44px] min-h-[44px] focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background rounded-full p-2 transition-transform active:scale-95"
        >
          <div className="w-20 h-20 rounded-full bg-danger/10 flex items-center justify-center">
            <AlertCircle size={32} className="text-danger" />
          </div>
          <span className="text-sm text-danger">Tap to retry</span>
        </button>
      );
  }
}

/**
 * Animated audio bars indicating that the agent is actively speaking.
 * Uses CSS keyframe animation with staggered delays for a natural
 * equalizer-like effect.
 */
function AudioBars() {
  return (
    <div className="flex items-end gap-1 h-8" aria-hidden="true">
      {[0, 1, 2, 3, 4].map((index) => (
        <span
          key={index}
          className="w-1 bg-accent-secondary rounded-full"
          style={{
            animation: 'voiceOverlayAudioBar 1.2s ease-in-out infinite',
            animationDelay: `${index * 0.15}s`,
            height: '8px',
          }}
        />
      ))}
      {/* Keyframe animation for audio bars — uses a globally-unique name to avoid collisions */}
      <style dangerouslySetInnerHTML={{ __html: `
        @keyframes voiceOverlayAudioBar {
          0%, 100% { height: 8px; }
          50% { height: 24px; }
        }
      ` }} />
    </div>
  );
}
