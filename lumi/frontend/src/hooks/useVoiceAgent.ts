'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { getIdToken, getAccessToken } from '@/lib/auth';
import { getIdentityPoolCredentials } from '@/lib/identityPool';
import { generatePresignedWsUrl } from '@/lib/sigv4WebSocket';
import { AGENTCORE_RUNTIME_ARN, AWS_REGION, withBase } from '@/lib/constants';

// Playback sample rate for Nova Sonic output (24kHz/16-bit/mono PCM)
const PLAYBACK_SAMPLE_RATE = 24000;

// Capture sample rate requested from getUserMedia
const CAPTURE_SAMPLE_RATE = 48000;

/**
 * Voice agent session status representing the current state of the
 * voice interaction lifecycle.
 */
export type VoiceAgentStatus =
  | 'idle'
  | 'connecting'
  | 'listening'
  | 'processing'
  | 'speaking'
  | 'error';

/**
 * State object exposed by the useVoiceAgent hook, representing the
 * current voice session status and transcript content.
 */
export interface VoiceAgentState {
  status: VoiceAgentStatus;
  userTranscript: string;
  agentTranscript: string;
  error: string | null;
}

/**
 * Return type of the useVoiceAgent hook, providing state and controls
 * for managing a voice conversation session.
 */
export interface UseVoiceAgentReturn {
  state: VoiceAgentState;
  startSession: () => Promise<void>;
  endSession: () => void;
  isConnected: boolean;
}

/**
 * Inbound WebSocket message types from the voice agent server.
 */
interface SessionStartedEvent {
  type: 'sessionStarted';
}

interface AudioOutputEvent {
  type: 'audioOutput';
  audioData: string;
}

interface UserTranscriptEvent {
  type: 'userTranscript';
  text: string;
  isFinal: boolean;
}

interface AgentTranscriptEvent {
  type: 'agentTranscript';
  text: string;
  isFinal: boolean;
}

interface ContentStartEvent {
  type: 'contentStart';
  role: 'ASSISTANT';
}

interface ContentEndEvent {
  type: 'contentEnd';
}

interface ToolUseEvent {
  type: 'toolUse';
  toolName: string;
}

interface ErrorEvent {
  type: 'error';
  code: string;
  message: string;
}

interface SessionEndedEvent {
  type: 'sessionEnded';
  reason: 'idle_timeout' | 'stream_error' | 'explicit' | 'server_shutdown';
}

type ServerEvent =
  | SessionStartedEvent
  | AudioOutputEvent
  | UserTranscriptEvent
  | AgentTranscriptEvent
  | ContentStartEvent
  | ContentEndEvent
  | ToolUseEvent
  | ErrorEvent
  | SessionEndedEvent;

/**
 * Converts an Int16Array PCM buffer to a base64-encoded string
 * suitable for sending over WebSocket as an audioInput event.
 */
function int16ArrayToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Decodes a base64-encoded string into an Int16Array representing
 * 24kHz/16-bit/mono PCM audio from Nova Sonic output.
 */
function base64ToInt16Array(base64: string): Int16Array {
  const binaryStr = atob(base64);
  const bytes = new Uint8Array(binaryStr.length);
  for (let i = 0; i < binaryStr.length; i++) {
    bytes[i] = binaryStr.charCodeAt(i);
  }
  return new Int16Array(bytes.buffer);
}

/**
 * Converts an Int16Array of PCM samples to a Float32Array normalized
 * to the -1.0 to 1.0 range expected by the Web Audio API.
 */
function int16ToFloat32(samples: Int16Array): Float32Array {
  const float32 = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    // Normalize: divide by 32768 for negative, 32767 for positive
    float32[i] = samples[i] < 0 ? samples[i] / 32768 : samples[i] / 32767;
  }
  return float32;
}

/**
 * Custom React hook managing a real-time voice conversation session with
 * the StayOS voice agent via WebSocket, AudioWorklet capture, and Web
 * Audio API playback.
 *
 * Handles the full lifecycle: WebSocket connection, microphone capture
 * (16kHz PCM via AudioWorklet), streaming audio to the server, receiving
 * and playing 24kHz PCM response audio, managing transcripts, and
 * implementing barge-in behavior.
 *
 * State machine:
 *   idle → connecting (on startSession)
 *   connecting → listening (on WebSocket open + sessionStarted received)
 *   listening → processing (on contentStart with ASSISTANT role)
 *   processing → speaking (on first audioOutput received)
 *   speaking → listening (on contentEnd)
 *   any → error (on WebSocket error, mic denied, stream error)
 *   any → idle (on endSession, sessionEnded, WebSocket close)
 */
export function useVoiceAgent(): UseVoiceAgentReturn {
  const [state, setState] = useState<VoiceAgentState>({
    status: 'idle',
    userTranscript: '',
    agentTranscript: '',
    error: null,
  });

  // Refs for mutable session resources (survive re-renders without stale closures)
  const wsRef = useRef<WebSocket | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);

  // Playback scheduling refs for gapless audio output
  const playbackContextRef = useRef<AudioContext | null>(null);
  const nextPlaybackTimeRef = useRef<number>(0);
  const activeSourcesRef = useRef<AudioBufferSourceNode[]>([]);

  // Track whether a session is actively connected
  const isConnectedRef = useRef<boolean>(false);

  /**
   * Stops all queued and playing AudioBufferSourceNodes.
   * Called during barge-in (user speaks while agent is speaking)
   * and on session cleanup.
   */
  const stopPlayback = useCallback(() => {
    for (const source of activeSourcesRef.current) {
      try {
        source.stop();
      } catch {
        // Source may have already ended — ignore
      }
    }
    activeSourcesRef.current = [];
    nextPlaybackTimeRef.current = 0;
  }, []);

  /**
   * Schedules a PCM audio chunk for gapless playback via the Web Audio API.
   * Converts base64 PCM (24kHz/16-bit/mono) to an AudioBuffer and chains
   * AudioBufferSourceNodes so audio plays without gaps between chunks.
   */
  const scheduleAudioPlayback = useCallback((audioData: string) => {
    const context = playbackContextRef.current;
    if (!context) return;

    // Decode base64 to Int16 PCM, then convert to Float32 for Web Audio
    const pcmSamples = base64ToInt16Array(audioData);
    const float32Samples = int16ToFloat32(pcmSamples);

    // Create an AudioBuffer at the Nova Sonic output sample rate (24kHz)
    const audioBuffer = context.createBuffer(1, float32Samples.length, PLAYBACK_SAMPLE_RATE);
    audioBuffer.getChannelData(0).set(float32Samples);

    // Create a source node and schedule it for gapless playback
    const source = context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(context.destination);

    // Schedule at the next available time (or now if nothing is queued)
    const startTime = Math.max(context.currentTime, nextPlaybackTimeRef.current);
    source.start(startTime);

    // Update the next playback time to chain the next chunk immediately after
    nextPlaybackTimeRef.current = startTime + audioBuffer.duration;

    // Track the source for cleanup/barge-in
    activeSourcesRef.current.push(source);

    // Remove from active sources when it finishes playing
    source.onended = () => {
      activeSourcesRef.current = activeSourcesRef.current.filter((s) => s !== source);
    };
  }, []);

  /**
   * Handles incoming WebSocket messages from the voice agent server.
   * Routes each event type to the appropriate state update or action.
   */
  const handleServerEvent = useCallback(
    (event: ServerEvent) => {
      switch (event.type) {
        case 'sessionStarted':
          // Connection confirmed — transition to listening
          setState((s) => ({ ...s, status: 'listening', error: null }));
          break;

        case 'contentStart':
          // Agent is about to speak — if currently speaking (barge-in scenario),
          // stop playback before processing new response
          setState((s) => {
            if (s.status === 'speaking') {
              stopPlayback();
            }
            return { ...s, status: 'processing', agentTranscript: '' };
          });
          break;

        case 'audioOutput':
          // First audio chunk transitions from processing to speaking
          setState((s) => ({
            ...s,
            status: s.status === 'processing' ? 'speaking' : s.status,
          }));
          scheduleAudioPlayback(event.audioData);
          break;

        case 'userTranscript':
          setState((s) => ({
            ...s,
            userTranscript: event.text,
          }));
          break;

        case 'agentTranscript':
          setState((s) => ({
            ...s,
            agentTranscript: event.text,
          }));
          break;

        case 'contentEnd':
          // Agent finished speaking — return to listening for next turn
          setState((s) => ({ ...s, status: 'listening' }));
          break;

        case 'toolUse':
          // Tool is being invoked server-side — remain in processing state
          break;

        case 'error':
          setState((s) => ({
            ...s,
            status: 'error',
            error: event.message || 'An error occurred',
          }));
          break;

        case 'sessionEnded':
          // Server closed the session (idle timeout, stream error, explicit close)
          stopPlayback();
          isConnectedRef.current = false;
          setState((s) => ({ ...s, status: 'idle', error: null }));
          break;
      }
    },
    [stopPlayback, scheduleAudioPlayback]
  );

  /**
   * Initializes the AudioWorklet for 16kHz PCM microphone capture.
   * Loads the worklet processor module, opens the mic via getUserMedia,
   * connects the MediaStreamSource to the worklet node, and wires up
   * the message handler to stream audio chunks to the WebSocket.
   */
  const initAudioCapture = useCallback(async (): Promise<void> => {
    // Create an AudioContext for capture (at the device sample rate)
    const audioContext = new AudioContext({ sampleRate: CAPTURE_SAMPLE_RATE });
    audioContextRef.current = audioContext;

    // Load the AudioWorklet processor module (served under /lumi via basePath)
    await audioContext.audioWorklet.addModule(withBase('/audio-worklet-processor.js'));

    // Request microphone access (mono, preferred 48kHz)
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: { ideal: CAPTURE_SAMPLE_RATE },
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
    mediaStreamRef.current = stream;

    // Create MediaStreamSource from the mic
    const source = audioContext.createMediaStreamSource(stream);

    // Create the AudioWorklet node for PCM capture processing
    const workletNode = new AudioWorkletNode(audioContext, 'pcm-capture-processor');
    workletNodeRef.current = workletNode;

    // Tell the worklet the actual source sample rate so it can calculate
    // the downsample ratio correctly
    workletNode.port.postMessage({
      type: 'init',
      sampleRate: audioContext.sampleRate,
    });

    // Listen for 320-sample Int16 PCM frames from the worklet
    workletNode.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        // Convert Int16 PCM buffer to base64 and send as audioInput event
        const audioData = int16ArrayToBase64(event.data);
        ws.send(JSON.stringify({ type: 'audioInput', audioData }));
      }
    };

    // Connect the audio graph: mic → worklet (captures PCM frames)
    source.connect(workletNode);

    // Connect worklet to destination to keep the audio graph alive
    // (output is silence — the worklet only captures, doesn't produce audible output)
    workletNode.connect(audioContext.destination);
  }, []);

  /**
   * Releases all audio capture resources: stops the mic stream,
   * disconnects the worklet, and closes the AudioContext.
   */
  const cleanupAudioCapture = useCallback(() => {
    // Stop all media stream tracks (releases the microphone)
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach((track) => track.stop());
      mediaStreamRef.current = null;
    }

    // Disconnect and close the capture AudioContext
    if (workletNodeRef.current) {
      workletNodeRef.current.disconnect();
      workletNodeRef.current = null;
    }
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
  }, []);

  /**
   * Releases all audio playback resources: stops active sources
   * and closes the playback AudioContext.
   */
  const cleanupPlayback = useCallback(() => {
    stopPlayback();
    if (playbackContextRef.current) {
      playbackContextRef.current.close();
      playbackContextRef.current = null;
    }
  }, [stopPlayback]);

  /**
   * Starts a new voice agent session:
   * 1. Validates auth token availability
   * 2. Exchanges ID token for temporary AWS credentials via Identity Pool
   * 3. Generates a SigV4 presigned WebSocket URL for AgentCore
   * 4. Opens WebSocket to AgentCore endpoint
   * 5. Initializes microphone capture via AudioWorklet
   * 6. Initializes playback AudioContext for response audio
   * 7. Sends identity message followed by sessionStart event
   */
  const startSession = useCallback(async (): Promise<void> => {
    // Get the Cognito ID token for Identity Pool credential exchange
    const idToken = getIdToken();
    if (!idToken) {
      setState((s) => ({
        ...s,
        status: 'error',
        error: 'Not authenticated. Please log in and try again.',
      }));
      return;
    }

    // Get the Cognito Access Token for identity verification by the agent container
    const accessToken = getAccessToken();
    if (!accessToken) {
      setState((s) => ({
        ...s,
        status: 'error',
        error: 'Not authenticated. Please log in and try again.',
      }));
      return;
    }

    // Validate the AgentCore Runtime ARN is configured
    if (!AGENTCORE_RUNTIME_ARN) {
      setState((s) => ({
        ...s,
        status: 'error',
        error: 'Voice service is not configured.',
      }));
      return;
    }

    // Transition to connecting state
    setState((s) => ({
      ...s,
      status: 'connecting',
      error: null,
      userTranscript: '',
      agentTranscript: '',
    }));

    try {
      // Initialize microphone capture before opening WebSocket
      // (so we can surface mic permission denial early)
      await initAudioCapture();
    } catch (err: unknown) {
      // Handle mic permission denial or other getUserMedia errors
      const message =
        err instanceof DOMException && err.name === 'NotAllowedError'
          ? 'Microphone access denied. Please allow microphone permission to use voice.'
          : 'Failed to access microphone. Please check your device settings.';

      cleanupAudioCapture();
      setState((s) => ({ ...s, status: 'error', error: message }));
      return;
    }

    // Exchange the ID token for temporary AWS credentials via Cognito Identity Pool
    let wsUrl: string;
    try {
      const credentials = await getIdentityPoolCredentials(idToken);

      // Generate a SigV4 presigned WebSocket URL for the AgentCore endpoint
      // (URL expires in 300s but connection persists once established)
      wsUrl = await generatePresignedWsUrl(
        AGENTCORE_RUNTIME_ARN,
        credentials,
        AWS_REGION
      );
    } catch (err: unknown) {
      // Credential exchange or SigV4 signing failed — clean up and show error
      const message =
        err instanceof Error
          ? err.message
          : 'Failed to obtain voice service credentials. Please log in again.';

      cleanupAudioCapture();
      setState((s) => ({ ...s, status: 'error', error: message }));
      return;
    }

    // Initialize playback AudioContext for agent audio output (24kHz)
    const playbackContext = new AudioContext({ sampleRate: PLAYBACK_SAMPLE_RATE });
    playbackContextRef.current = playbackContext;
    nextPlaybackTimeRef.current = 0;

    // Open the WebSocket connection to AgentCore (SigV4 auth is in the URL query params)
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      isConnectedRef.current = true;
      // Send Access Token as first message for identity verification by the agent container
      // (AgentCore uses SigV4 for transport auth; this identifies which GM is speaking)
      ws.send(JSON.stringify({ type: 'identity', accessToken }));
      // Send sessionStart event to initiate the Nova Sonic stream on the server
      ws.send(JSON.stringify({ type: 'sessionStart' }));
    };

    ws.onmessage = (event: MessageEvent) => {
      try {
        const serverEvent = JSON.parse(event.data as string) as ServerEvent;
        handleServerEvent(serverEvent);
      } catch {
        // Malformed message from server — ignore
      }
    };

    ws.onerror = () => {
      isConnectedRef.current = false;
      cleanupAudioCapture();
      cleanupPlayback();
      setState((s) => ({
        ...s,
        status: 'error',
        error: 'Connection to voice service failed. Please try again.',
      }));
    };

    ws.onclose = () => {
      isConnectedRef.current = false;
      cleanupAudioCapture();
      cleanupPlayback();
      setState((s) => {
        // Only transition to idle if we're not already in an error state
        if (s.status === 'error') return s;
        return { ...s, status: 'idle' };
      });
    };
  }, [initAudioCapture, cleanupAudioCapture, cleanupPlayback, handleServerEvent]);

  /**
   * Ends the current voice session gracefully:
   * sends a sessionEnd event to the server, closes the WebSocket,
   * and releases all audio resources.
   */
  const endSession = useCallback((): void => {
    // Send sessionEnd to the server if connected
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'sessionEnd' }));
      ws.close();
    }
    wsRef.current = null;
    isConnectedRef.current = false;

    // Release all audio resources
    cleanupAudioCapture();
    cleanupPlayback();

    // Reset state to idle
    setState((s) => ({ ...s, status: 'idle', error: null }));
  }, [cleanupAudioCapture, cleanupPlayback]);

  // Clean up all resources on unmount
  useEffect(() => {
    return () => {
      // Close WebSocket if still open
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      isConnectedRef.current = false;

      // Release audio capture resources
      if (mediaStreamRef.current) {
        mediaStreamRef.current.getTracks().forEach((track) => track.stop());
      }
      if (audioContextRef.current) {
        audioContextRef.current.close();
      }

      // Release playback resources
      stopPlayback();
      if (playbackContextRef.current) {
        playbackContextRef.current.close();
      }
    };
  }, [stopPlayback]);

  return {
    state,
    startSession,
    endSession,
    isConnected: isConnectedRef.current,
  };
}
