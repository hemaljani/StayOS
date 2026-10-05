/**
 * Component tests for VoiceOverlay.
 *
 * Validates that the VoiceOverlay component renders correct visual indicators
 * for each voice agent status, provides proper accessibility attributes,
 * handles keyboard interactions (Escape to close), and displays transcripts.
 *
 * **Property 11: Voice Overlay State Rendering** — for each status value,
 * verify correct visual indicator rendered.
 *
 * **Validates: Requirements 7.2, 7.3, 7.8**
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';

// --- Mock useVoiceAgent hook ---

const mockStartSession = vi.fn().mockResolvedValue(undefined);
const mockEndSession = vi.fn();

/**
 * Default mock state returned by useVoiceAgent.
 * Tests override this via mockUseVoiceAgentReturn before rendering.
 */
let mockUseVoiceAgentReturn = {
  state: {
    status: 'idle' as string,
    userTranscript: '',
    agentTranscript: '',
    error: null as string | null,
  },
  startSession: mockStartSession,
  endSession: mockEndSession,
  isConnected: false,
};

vi.mock('@/hooks/useVoiceAgent', () => ({
  useVoiceAgent: () => mockUseVoiceAgentReturn,
}));

// --- Mock createPortal to render inline (avoids needing document.body portal) ---

vi.mock('react-dom', async () => {
  const actual = await vi.importActual<typeof import('react-dom')>('react-dom');
  return {
    ...actual,
    createPortal: (node: React.ReactNode) => node,
  };
});

// --- Import component under test after mocks are set up ---

import VoiceOverlay from '@/components/VoiceOverlay';

// --- Setup and teardown ---

beforeEach(() => {
  // Reset to default state before each test
  mockUseVoiceAgentReturn = {
    state: {
      status: 'idle',
      userTranscript: '',
      agentTranscript: '',
      error: null,
    },
    startSession: mockStartSession,
    endSession: mockEndSession,
    isConnected: false,
  };
  mockStartSession.mockClear();
  mockEndSession.mockClear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// --- Tests ---

describe('Property 11: Voice Overlay State Rendering', () => {
  describe('overlay visibility and dialog semantics', () => {
    it('renders with role="dialog" and aria-modal="true" when isOpen=true', () => {
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      const dialog = screen.getByRole('dialog');
      expect(dialog).toBeInTheDocument();
      expect(dialog).toHaveAttribute('aria-modal', 'true');
      expect(dialog).toHaveAttribute('aria-label', 'LUMI Voice Assistant');
    });

    it('does not render when isOpen=false', () => {
      render(<VoiceOverlay isOpen={false} onClose={vi.fn()} />);

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  describe('idle state', () => {
    it('shows "Tap to speak" text', () => {
      mockUseVoiceAgentReturn.state.status = 'idle';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(screen.getByText('Tap to speak')).toBeInTheDocument();
    });

    it('renders mic button with correct aria-label', () => {
      mockUseVoiceAgentReturn.state.status = 'idle';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(screen.getByRole('button', { name: 'Tap to speak' })).toBeInTheDocument();
    });
  });

  describe('listening state', () => {
    it('shows "Listening..." text', () => {
      mockUseVoiceAgentReturn.state.status = 'listening';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(screen.getByText('Listening...')).toBeInTheDocument();
    });

    it('renders mic button with listening aria-label', () => {
      mockUseVoiceAgentReturn.state.status = 'listening';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(
        screen.getByRole('button', { name: 'Listening. Tap to stop.' })
      ).toBeInTheDocument();
    });
  });

  describe('processing state', () => {
    it('shows "Thinking..." text', () => {
      mockUseVoiceAgentReturn.state.status = 'processing';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(screen.getByText('Thinking...')).toBeInTheDocument();
    });
  });

  describe('speaking state', () => {
    it('shows "Speaking..." text', () => {
      mockUseVoiceAgentReturn.state.status = 'speaking';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(screen.getByText('Speaking...')).toBeInTheDocument();
    });

    it('renders mic button with speaking aria-label', () => {
      mockUseVoiceAgentReturn.state.status = 'speaking';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(
        screen.getByRole('button', { name: 'Agent is speaking. Tap to stop.' })
      ).toBeInTheDocument();
    });
  });

  describe('error state', () => {
    it('shows error message text', () => {
      mockUseVoiceAgentReturn.state.status = 'error';
      mockUseVoiceAgentReturn.state.error =
        'Microphone access denied. Please allow microphone permission to use voice.';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(
        screen.getByText(
          'Microphone access denied. Please allow microphone permission to use voice.'
        )
      ).toBeInTheDocument();
    });

    it('shows "Tap to retry" text in error state', () => {
      mockUseVoiceAgentReturn.state.status = 'error';
      mockUseVoiceAgentReturn.state.error = 'Some error occurred';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(screen.getByText('Tap to retry')).toBeInTheDocument();
    });

    it('renders retry button with error aria-label', () => {
      mockUseVoiceAgentReturn.state.status = 'error';
      mockUseVoiceAgentReturn.state.error = 'Connection failed';
      render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

      expect(
        screen.getByRole('button', { name: 'Error occurred. Tap to retry.' })
      ).toBeInTheDocument();
    });
  });
});

describe('Accessibility', () => {
  it('close button has aria-label', () => {
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    // When not connected, the close button should say "Close voice assistant"
    expect(
      screen.getByRole('button', { name: 'Close voice assistant' })
    ).toBeInTheDocument();
  });

  it('close button shows "Stop session and close" when connected', () => {
    mockUseVoiceAgentReturn.isConnected = true;
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    expect(
      screen.getByRole('button', { name: 'Stop session and close' })
    ).toBeInTheDocument();
  });

  it('Escape key calls onClose', () => {
    const onClose = vi.fn();
    render(<VoiceOverlay isOpen={true} onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape key also calls endSession when connected', () => {
    const onClose = vi.fn();
    mockUseVoiceAgentReturn.isConnected = true;
    render(<VoiceOverlay isOpen={true} onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(mockEndSession).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('mic button has aria-label in idle state', () => {
    mockUseVoiceAgentReturn.state.status = 'idle';
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    const micButton = screen.getByRole('button', { name: 'Tap to speak' });
    expect(micButton).toHaveAttribute('aria-label', 'Tap to speak');
  });

  it('overlay has aria-live region for transcript updates', () => {
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    const dialog = screen.getByRole('dialog');
    const liveRegion = dialog.querySelector('[aria-live="polite"]');
    expect(liveRegion).toBeInTheDocument();
  });
});

describe('Transcript display', () => {
  it('displays user transcript text', () => {
    mockUseVoiceAgentReturn.state.status = 'listening';
    mockUseVoiceAgentReturn.state.userTranscript = 'What is my occupancy today?';
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    expect(screen.getByText('What is my occupancy today?')).toBeInTheDocument();
  });

  it('displays agent transcript text', () => {
    mockUseVoiceAgentReturn.state.status = 'speaking';
    mockUseVoiceAgentReturn.state.agentTranscript =
      'Your occupancy today is 85%.';
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    expect(screen.getByText('Your occupancy today is 85%.')).toBeInTheDocument();
  });

  it('displays both user and agent transcripts simultaneously', () => {
    mockUseVoiceAgentReturn.state.status = 'speaking';
    mockUseVoiceAgentReturn.state.userTranscript = 'How many VIPs arrive today?';
    mockUseVoiceAgentReturn.state.agentTranscript =
      'You have 3 VIP arrivals today.';
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    expect(screen.getByText('How many VIPs arrive today?')).toBeInTheDocument();
    expect(
      screen.getByText('You have 3 VIP arrivals today.')
    ).toBeInTheDocument();
  });

  it('does not display transcript areas when transcripts are empty', () => {
    mockUseVoiceAgentReturn.state.status = 'idle';
    mockUseVoiceAgentReturn.state.userTranscript = '';
    mockUseVoiceAgentReturn.state.agentTranscript = '';
    render(<VoiceOverlay isOpen={true} onClose={vi.fn()} />);

    // The live region container exists but should have no paragraph children
    const dialog = screen.getByRole('dialog');
    const liveRegion = dialog.querySelector('[aria-live="polite"]');
    expect(liveRegion).toBeInTheDocument();
    const paragraphs = liveRegion!.querySelectorAll('p');
    expect(paragraphs.length).toBe(0);
  });
});
