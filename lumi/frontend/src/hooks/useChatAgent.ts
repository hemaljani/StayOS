'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { getIdToken, getAccessToken } from '@/lib/auth';
import { getIdentityPoolCredentials } from '@/lib/identityPool';
import { generatePresignedWsUrl } from '@/lib/sigv4WebSocket';
import { CHAT_RUNTIME_ARN, AWS_REGION } from '@/lib/constants';

/**
 * Chat agent session status representing the current state of the
 * WebSocket connection lifecycle.
 */
export type ChatAgentStatus = 'idle' | 'connecting' | 'connected' | 'error';

/**
 * A single message in the chat conversation thread, either from the
 * GM (user) or from LUMI (assistant).
 */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  timestamp: Date;
}

/**
 * State object exposed by the useChatAgent hook, representing the
 * current connection status and conversation thread.
 */
export interface ChatAgentState {
  status: ChatAgentStatus;
  messages: ChatMessage[];
  isAgentTyping: boolean;
  error: string | null;
}

/**
 * Return type of the useChatAgent hook, providing state and controls
 * for managing a text chat session.
 */
export interface UseChatAgentReturn {
  state: ChatAgentState;
  connect: () => Promise<void>;
  disconnect: () => void;
  sendMessage: (text: string) => void;
}

/**
 * Inbound WebSocket message types from the chat agent server.
 */
interface SessionStartedEvent {
  type: 'sessionStarted';
}

interface MessageStartEvent {
  type: 'messageStart';
}

interface MessageDeltaEvent {
  type: 'messageDelta';
  text: string;
}

interface MessageEndEvent {
  type: 'messageEnd';
}

interface ErrorEvent {
  type: 'error';
  code: string;
  message: string;
}

interface SessionEndedEvent {
  type: 'sessionEnded';
  reason: string;
}

type ServerEvent =
  | SessionStartedEvent
  | MessageStartEvent
  | MessageDeltaEvent
  | MessageEndEvent
  | ErrorEvent
  | SessionEndedEvent;

/**
 * Generates a random client-side message ID for React keys and state
 * tracking. Not used for server correlation.
 */
function generateMessageId(): string {
  return crypto.randomUUID();
}

/**
 * Custom React hook managing a text chat session with the StayOS chat
 * agent via WebSocket.
 *
 * Handles the full lifecycle: WebSocket connection (SigV4-signed via
 * Cognito Identity Pool credentials), sending user messages, streaming
 * assistant response chunks into accumulated messages, and tracking
 * typing/connection state.
 *
 * State machine:
 *   idle → connecting (on connect)
 *   connecting → connected (on WebSocket open + sessionStarted received)
 *   any → error (on WebSocket error, auth failure, or server error event)
 *   any → idle (on disconnect, sessionEnded, WebSocket close)
 */
export function useChatAgent(): UseChatAgentReturn {
  const [state, setState] = useState<ChatAgentState>({
    status: 'idle',
    messages: [],
    isAgentTyping: false,
    error: null,
  });

  // Ref for the mutable WebSocket reference (survives re-renders without stale closures)
  const wsRef = useRef<WebSocket | null>(null);

  // Track whether a session is actively connected
  const isConnectedRef = useRef<boolean>(false);

  // Tracks the message id of the in-progress assistant message so that
  // messageDelta events can append to the correct message without relying
  // on stale state closures.
  const activeAssistantMessageIdRef = useRef<string | null>(null);

  /**
   * Handles incoming WebSocket messages from the chat agent server.
   * Routes each event type to the appropriate state update.
   */
  const handleServerEvent = useCallback((event: ServerEvent) => {
    switch (event.type) {
      case 'sessionStarted':
        // Connection confirmed — transition to connected
        setState((s) => ({ ...s, status: 'connected', error: null }));
        break;

      case 'messageStart': {
        // Agent began generating a response — create an empty assistant
        // message placeholder that subsequent deltas will append to.
        const messageId = generateMessageId();
        activeAssistantMessageIdRef.current = messageId;
        setState((s) => ({
          ...s,
          isAgentTyping: true,
          messages: [
            ...s.messages,
            { id: messageId, role: 'assistant', text: '', timestamp: new Date() },
          ],
        }));
        break;
      }

      case 'messageDelta': {
        // Append the streamed text chunk to the active assistant message
        const activeId = activeAssistantMessageIdRef.current;
        if (!activeId) break;
        setState((s) => ({
          ...s,
          messages: s.messages.map((message) =>
            message.id === activeId
              ? { ...message, text: message.text + event.text }
              : message
          ),
        }));
        break;
      }

      case 'messageEnd':
        // Agent finished streaming the response
        activeAssistantMessageIdRef.current = null;
        setState((s) => ({ ...s, isAgentTyping: false }));
        break;

      case 'error':
        setState((s) => ({
          ...s,
          status: 'error',
          error: event.message || 'An error occurred',
        }));
        break;

      case 'sessionEnded':
        // Server closed the session (idle timeout, explicit close, etc.)
        isConnectedRef.current = false;
        setState((s) => ({ ...s, status: 'idle', isAgentTyping: false, error: null }));
        break;
    }
  }, []);

  /**
   * Connects a new chat session:
   * 1. Validates auth token availability
   * 2. Exchanges ID token for temporary AWS credentials via Identity Pool
   * 3. Generates a SigV4 presigned WebSocket URL for the chat agent runtime
   * 4. Opens WebSocket to the chat agent endpoint
   * 5. Sends the identity message with the Access Token on open
   */
  const connect = useCallback(async (): Promise<void> => {
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

    // Validate the chat agent Runtime ARN is configured
    if (!CHAT_RUNTIME_ARN) {
      setState((s) => ({
        ...s,
        status: 'error',
        error: 'Chat service is not configured.',
      }));
      return;
    }

    // Transition to connecting state
    setState((s) => ({ ...s, status: 'connecting', error: null }));

    // Exchange the ID token for temporary AWS credentials via Cognito Identity Pool
    let wsUrl: string;
    try {
      const credentials = await getIdentityPoolCredentials(idToken);

      // Generate a SigV4 presigned WebSocket URL for the chat agent endpoint
      // (URL expires in 300s but connection persists once established)
      wsUrl = await generatePresignedWsUrl(CHAT_RUNTIME_ARN, credentials, AWS_REGION);
    } catch (err: unknown) {
      // Credential exchange or SigV4 signing failed — show error
      const message =
        err instanceof Error
          ? err.message
          : 'Failed to obtain chat service credentials. Please log in again.';

      setState((s) => ({ ...s, status: 'error', error: message }));
      return;
    }

    // Open the WebSocket connection to the chat agent (SigV4 auth is in the URL query params)
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      isConnectedRef.current = true;
      // Send Access Token as first message for identity verification by the agent container
      // (the chat agent uses SigV4 for transport auth; this identifies which GM is chatting)
      ws.send(JSON.stringify({ type: 'identity', accessToken }));
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
      setState((s) => ({
        ...s,
        status: 'error',
        error: 'Connection to chat service failed. Please try again.',
      }));
    };

    ws.onclose = () => {
      isConnectedRef.current = false;
      setState((s) => {
        // Only transition to idle if we're not already in an error state
        if (s.status === 'error') return s;
        return { ...s, status: 'idle', isAgentTyping: false };
      });
    };
  }, [handleServerEvent]);

  /**
   * Sends a text message to the chat agent.
   * Optimistically appends the user's message to the conversation thread,
   * then transmits it over the WebSocket.
   */
  const sendMessage = useCallback((text: string): void => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    // Optimistically add the user message to the thread before the round trip
    setState((s) => ({
      ...s,
      messages: [
        ...s.messages,
        { id: generateMessageId(), role: 'user', text, timestamp: new Date() },
      ],
    }));

    ws.send(JSON.stringify({ type: 'message', text }));
  }, []);

  /**
   * Ends the current chat session gracefully:
   * sends a sessionEnd event to the server, closes the WebSocket,
   * and resets state to idle.
   */
  const disconnect = useCallback((): void => {
    // Send sessionEnd to the server if connected
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'sessionEnd' }));
      ws.close();
    }
    wsRef.current = null;
    isConnectedRef.current = false;
    activeAssistantMessageIdRef.current = null;

    // Reset state to idle
    setState((s) => ({ ...s, status: 'idle', isAgentTyping: false, error: null }));
  }, []);

  // Clean up the WebSocket on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      isConnectedRef.current = false;
    };
  }, []);

  return {
    state,
    connect,
    disconnect,
    sendMessage,
  };
}
