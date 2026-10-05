'use client';

import VoiceOverlay from '@/components/VoiceOverlay';

/**
 * Voice Assistant page - renders the full push-to-talk voice interface.
 * Uses the same VoiceOverlay component that the bottom nav mic button triggers,
 * but displayed inline as a full page rather than as a modal overlay.
 */
export default function VoiceAssistantPage() {
  return <VoiceOverlay isOpen={true} onClose={() => window.history.back()} />;
}
