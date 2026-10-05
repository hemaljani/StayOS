/**
 * Unit tests for the useVoiceAgent hook.
 *
 * Validates state transitions through the voice interaction lifecycle:
 * idle → connecting → listening → processing → speaking, and error states.
 * Mocks WebSocket, AudioContext, AudioWorkletNode, and navigator.mediaDevices.
 *
 * **Validates: Requirements 7.6, 8.2**
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useVoiceAgent } from '@/hooks/useVoiceAgent';

// --- Mock constants and auth modules ---

vi.mock('@/lib/auth', () => ({
  getIdToken: vi.fn(() => 'mock-id-token'),
  getAccessToken: vi.fn(() => 'mock-access-token'),
}));

vi.mock('@/lib/constants', () => ({
  AGENTCORE_RUNTIME_ARN: 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/stayos-voice-agent',
  AWS_REGION: 'us-east-1',
  // LUMI is served under /lumi; the hook wraps the AudioWorklet module URL with
  // withBase(). Mirror the real prefixing so the module path resolves in tests.
  BASE_PATH: '/lumi',
  withBase: (path: string) => {
    const normalized = path.startsWith('/') ? path : `/${path}`;
    if (normalized === '/lumi' || normalized.startsWith('/lumi/')) return normalized;
    return `/lumi${normalized}`;
  },
}));

vi.mock('@/lib/identityPool', () => ({
  getIdentityPoolCredentials: vi.fn(() =>
    Promise.resolve({
      accessKeyId: 'AKIAIOSFODNN7EXAMPLE',
      secretAccessKey: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
      sessionToken: 'mock-session-token',
      expiration: new Date(Date.now() + 3600 * 1000),
    })
  ),
}));

vi.mock('@/lib/sigv4WebSocket', () => ({
  generatePresignedWsUrl: vi.fn(() =>
    Promise.resolve('wss://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/mock-arn/ws?X-Amz-Signature=mock')
  ),
}));

// --- Mock WebSocket ---

/**
 * Simulated WebSocket instance that allows tests to trigger
 * onopen, onmessage, onerror, and onclose callbacks.
 */
class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  url: string;
  readyState: number = MockWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  sentMessages: string[] = [];

  constructor(url: string) {
    this.url = url;
    // Store the instance so tests can access it
    mockWebSocketInstances.push(this);
  }

  send(data: string): void {
    this.sentMessages.push(data);
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    if (this.onclose) {
      this.onclose(new CloseEvent('close'));
    }
  }

  // Test helper: simulate server opening the connection
  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    if (this.onopen) {
      this.onopen(new Event('open'));
    }
  }

  // Test helper: simulate receiving a server message
  simulateMessage(data: object): void {
    if (this.onmessage) {
      this.onmessage(new MessageEvent('message', { data: JSON.stringify(data) }));
    }
  }

  // Test helper: simulate a WebSocket error
  simulateError(): void {
    if (this.onerror) {
      this.onerror(new Event('error'));
    }
  }
}

let mockWebSocketInstances: MockWebSocket[] = [];

// --- Mock AudioContext and related Web Audio APIs ---

const mockAudioWorkletAddModule = vi.fn().mockResolvedValue(undefined);
const mockMediaStreamSourceConnect = vi.fn();
const mockWorkletNodeConnect = vi.fn();
const mockWorkletNodeDisconnect = vi.fn();
const mockAudioContextClose = vi.fn().mockResolvedValue(undefined);
const mockPlaybackContextClose = vi.fn().mockResolvedValue(undefined);
const mockMediaTrackStop = vi.fn();

let workletNodePortOnmessage: ((event: MessageEvent) => void) | null = null;

class MockAudioContext {
  sampleRate = 48000;
  currentTime = 0;
  destination = {};
  audioWorklet = { addModule: mockAudioWorkletAddModule };

  createMediaStreamSource = vi.fn(() => ({
    connect: mockMediaStreamSourceConnect,
  }));

  createBuffer = vi.fn((_channels: number, length: number, sampleRate: number) => ({
    duration: length / sampleRate,
    getChannelData: () => new Float32Array(length),
  }));

  createBufferSource = vi.fn(() => ({
    buffer: null,
    connect: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
    onended: null as (() => void) | null,
  }));

  close = mockAudioContextClose;
}

class MockAudioWorkletNode {
  port = {
    postMessage: vi.fn(),
    set onmessage(handler: ((event: MessageEvent) => void) | null) {
      workletNodePortOnmessage = handler;
    },
    get onmessage() {
      return workletNodePortOnmessage;
    },
  };
  connect = mockWorkletNodeConnect;
  disconnect = mockWorkletNodeDisconnect;
}

// --- Mock getUserMedia ---

const mockGetUserMedia = vi.fn();

// --- Setup and teardown ---

beforeEach(() => {
  mockWebSocketInstances = [];
  workletNodePortOnmessage = null;

  // Mock global WebSocket constructor
  vi.stubGlobal('WebSocket', MockWebSocket);

  // Mock AudioContext (used for both capture and playback)
  vi.stubGlobal('AudioContext', MockAudioContext);

  // Mock AudioWorkletNode
  vi.stubGlobal('AudioWorkletNode', MockAudioWorkletNode);

  // Mock navigator.mediaDevices.getUserMedia
  mockGetUserMedia.mockResolvedValue({
    getTracks: () => [{ stop: mockMediaTrackStop }],
  });
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: mockGetUserMedia },
    configurable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// --- Tests ---

describe('Property 11: Voice Overlay State Rendering (partial) — useVoiceAgent state transitions', () => {
  describe('initial state', () => {
    it('should start in idle status with empty transcripts and no error', () => {
      const { result } = renderHook(() => useVoiceAgent());

      expect(result.current.state.status).toBe('idle');
      expect(result.current.state.userTranscript).toBe('');
      expect(result.current.state.agentTranscript).toBe('');
      expect(result.current.state.error).toBeNull();
      expect(result.current.isConnected).toBe(false);
    });
  });

  describe('startSession transitions to connecting', () => {
    it('should transition to connecting status when startSession is called', async () => {
      const { result } = renderHook(() => useVoiceAgent());

      await act(async () => {
        // Start the session — triggers mic access + WebSocket open
        result.current.startSession();
        // Allow the mic promise to resolve synchronously in the mock
        await Promise.resolve();
      });

      // After mic is acquired and WebSocket is created, status should be connecting
      // (waiting for sessionStarted event from server)
      expect(result.current.state.status).toBe('connecting');
      expect(result.current.state.error).toBeNull();
    });
  });

  describe('mic permission denied sets error state', () => {
    it('should set error status with helpful message when mic access is denied', async () => {
      // Simulate getUserMedia rejecting with NotAllowedError
      const notAllowedError = new DOMException(
        'Permission denied',
        'NotAllowedError'
      );
      mockGetUserMedia.mockRejectedValueOnce(notAllowedError);

      const { result } = renderHook(() => useVoiceAgent());

      await act(async () => {
        await result.current.startSession();
      });

      expect(result.current.state.status).toBe('error');
      expect(result.current.state.error).toBe(
        'Microphone access denied. Please allow microphone permission to use voice.'
      );
    });

    it('should set generic error when mic fails for other reasons', async () => {
      // Simulate a non-NotAllowedError (e.g., device not found)
      mockGetUserMedia.mockRejectedValueOnce(
        new DOMException('No device found', 'NotFoundError')
      );

      const { result } = renderHook(() => useVoiceAgent());

      await act(async () => {
        await result.current.startSession();
      });

      expect(result.current.state.status).toBe('error');
      expect(result.current.state.error).toBe(
        'Failed to access microphone. Please check your device settings.'
      );
    });
  });

  describe('endSession returns to idle', () => {
    it('should return to idle status when endSession is called', async () => {
      const { result } = renderHook(() => useVoiceAgent());

      // Start a session and get to connecting state
      await act(async () => {
        await result.current.startSession();
      });

      // Simulate WebSocket opening
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateOpen();
      });

      // End the session
      act(() => {
        result.current.endSession();
      });

      expect(result.current.state.status).toBe('idle');
      expect(result.current.state.error).toBeNull();
    });
  });

  describe('audioOutput event transitions from processing to speaking', () => {
    it('should transition to speaking when audioOutput arrives during processing', async () => {
      const { result } = renderHook(() => useVoiceAgent());

      // Start session
      await act(async () => {
        await result.current.startSession();
      });

      // Open WebSocket
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateOpen();
      });

      // Server confirms session started → listening
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'sessionStarted' });
      });
      expect(result.current.state.status).toBe('listening');

      // Agent starts responding → processing
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'contentStart', role: 'ASSISTANT' });
      });
      expect(result.current.state.status).toBe('processing');

      // First audio output arrives → speaking
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        // Send a tiny base64-encoded audio chunk (2 bytes = 1 sample of silence)
        ws.simulateMessage({ type: 'audioOutput', audioData: 'AAA=' });
      });
      expect(result.current.state.status).toBe('speaking');
    });
  });

  describe('sessionEnded event returns to idle', () => {
    it('should return to idle when server sends sessionEnded', async () => {
      const { result } = renderHook(() => useVoiceAgent());

      // Start and connect
      await act(async () => {
        await result.current.startSession();
      });
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateOpen();
      });
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'sessionStarted' });
      });
      expect(result.current.state.status).toBe('listening');

      // Server ends session (e.g., idle timeout)
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'sessionEnded', reason: 'idle_timeout' });
      });

      expect(result.current.state.status).toBe('idle');
      expect(result.current.state.error).toBeNull();
    });
  });

  describe('error event sets error status with message', () => {
    it('should set error status when server sends an error event', async () => {
      const { result } = renderHook(() => useVoiceAgent());

      // Start and connect
      await act(async () => {
        await result.current.startSession();
      });
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateOpen();
      });
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'sessionStarted' });
      });

      // Server sends an error
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({
          type: 'error',
          code: 'STREAM_DISCONNECTED',
          message: 'The voice session was interrupted. Please try again.',
        });
      });

      expect(result.current.state.status).toBe('error');
      expect(result.current.state.error).toBe(
        'The voice session was interrupted. Please try again.'
      );
    });
  });

  describe('full state transition cycle: idle → connecting → listening → processing → speaking', () => {
    it('should follow the complete happy-path state machine', async () => {
      const { result } = renderHook(() => useVoiceAgent());

      // 1. idle
      expect(result.current.state.status).toBe('idle');

      // 2. idle → connecting (startSession called)
      await act(async () => {
        await result.current.startSession();
      });
      expect(result.current.state.status).toBe('connecting');

      // 3. connecting → listening (WebSocket opens + sessionStarted received)
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateOpen();
      });
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'sessionStarted' });
      });
      expect(result.current.state.status).toBe('listening');

      // 4. listening → processing (contentStart with ASSISTANT role)
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'contentStart', role: 'ASSISTANT' });
      });
      expect(result.current.state.status).toBe('processing');

      // 5. processing → speaking (first audioOutput received)
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'audioOutput', audioData: 'AAA=' });
      });
      expect(result.current.state.status).toBe('speaking');

      // 6. speaking → listening (contentEnd)
      await act(async () => {
        const ws = mockWebSocketInstances[0];
        ws.simulateMessage({ type: 'contentEnd' });
      });
      expect(result.current.state.status).toBe('listening');
    });
  });
});
