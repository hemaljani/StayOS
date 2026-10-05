'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, Send, AlertCircle } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useChatAgent } from '@/hooks/useChatAgent';
import type { ChatMessage } from '@/hooks/useChatAgent';

/**
 * Suggested example questions shown as tappable chips so a GM always has a
 * quick way to ask something, even before they've typed anything - mirrors
 * the "Try asking" suggestions shown in VoiceOverlay.tsx so both interaction
 * modes surface the same example questions.
 */
const EXAMPLE_QUESTIONS: readonly string[] = [
  "What's my occupancy today?",
  'Any VIP arrivals?',
  "How's revenue trending?",
  'Rooms out of order?',
  'Open work orders?',
];

/**
 * Props for the ChatPanel component controlling its visibility
 * and providing a close callback.
 */
interface ChatPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

/**
 * Full-viewport modal panel for text-based chat with the StayOS LUMI
 * assistant.
 *
 * Renders as a React portal so it sits above all other UI (z-[70], same
 * level as VoiceOverlay since only one of the two can be open at a time).
 * Connects to the chat agent via `useChatAgent` when opened and disconnects
 * on close/unmount. Provides a scrollable message thread with auto-scroll,
 * a typing indicator, and a text input area with Enter-to-send behavior.
 *
 * Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9
 */
export default function ChatPanel({ isOpen, onClose }: ChatPanelProps) {
  const { state, connect, disconnect, sendMessage } = useChatAgent();
  const [inputText, setInputText] = useState<string>('');

  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  /**
   * Handles closing the panel. The connection lifecycle effect below
   * takes care of disconnecting once `isOpen` flips to false.
   */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  // Connection lifecycle: connect when the panel opens, disconnect when it
  // closes or unmounts. `connect`/`disconnect` are stable (useCallback with
  // empty/stable deps in the hook), so this effect only re-runs on isOpen
  // changes.
  useEffect(() => {
    if (isOpen) {
      void connect();
    }
    return () => {
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  // Focus the text input shortly after the panel opens
  useEffect(() => {
    if (isOpen) {
      const timer = setTimeout(() => {
        inputRef.current?.focus();
      }, 50);
      return () => clearTimeout(timer);
    }
  }, [isOpen]);

  // Auto-scroll to the latest message whenever the thread updates
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [state.messages, state.isAgentTyping]);

  // Keyboard handler: Escape to close, Tab focus trap within the panel
  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        handleClose();
        return;
      }

      // Focus trap: Tab cycles between focusable elements in the panel
      if (event.key === 'Tab') {
        const focusableElements = panelRef.current?.querySelectorAll(
          'button:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
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
  }, [isOpen, handleClose]);

  /**
   * Sends the current input text to the chat agent and clears the input.
   * No-ops if the input is empty (after trimming) or the session isn't
   * connected yet.
   */
  const handleSend = useCallback(() => {
    const trimmed = inputText.trim();
    if (!trimmed || state.status !== 'connected') return;

    sendMessage(trimmed);
    setInputText('');
  }, [inputText, state.status, sendMessage]);

  /**
   * Handles key presses in the text area: Enter sends the message,
   * Shift+Enter inserts a newline (default textarea behavior).
   */
  const handleInputKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  /**
   * Sends a suggested example question directly, bypassing the text
   * input. No-ops if the session isn't connected yet (mirrors the
   * `canSend` guard used for the composer's send button).
   */
  const handleExampleClick = useCallback(
    (question: string) => {
      if (state.status !== 'connected') return;
      sendMessage(question);
    },
    [state.status, sendMessage]
  );

  // Don't render anything if closed
  if (!isOpen) return null;

  const canSend = inputText.trim().length > 0 && state.status === 'connected';

  const panel = (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal="true"
      aria-label="LUMI Chat"
      className="fixed inset-0 z-[70] flex flex-col bg-background"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 bg-surface">
        <h2 className="text-lg font-semibold text-ink">LUMI Chat</h2>
        <button
          ref={closeButtonRef}
          type="button"
          onClick={handleClose}
          aria-label="Close chat"
          className="min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full text-gray-400 hover:text-ink hover:bg-surface/80 focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background transition-colors"
        >
          <X size={22} />
        </button>
      </div>

      {/* Message thread */}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="flex-1 overflow-y-auto px-4 py-4 space-y-4"
      >
        {state.messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        {/* Typing/thinking indicator, styled like an assistant bubble */}
        {state.isAgentTyping && (
          <div className="text-left">
            <div
              className="inline-flex items-center gap-1 bg-surface rounded-2xl rounded-bl-sm px-4 py-3"
              aria-label="LUMI is typing"
            >
              <TypingDots />
            </div>
          </div>
        )}

        {/* Scroll anchor */}
        <div ref={messagesEndRef} />
      </div>

      {/* Error banner */}
      {state.status === 'error' && state.error && (
        <div className="mx-4 mb-3 bg-danger/10 border border-danger/30 rounded-xl px-4 py-3 flex items-start gap-3">
          <AlertCircle size={18} className="text-danger mt-0.5 shrink-0" />
          <p className="text-sm text-gray-300">{state.error}</p>
        </div>
      )}

      {/* Suggested questions - always visible above the composer, not just
          on first load, so a GM always has a quick way to ask something */}
      {state.status !== 'error' && (
        <div className="px-4 pt-3 border-t border-gray-800 bg-surface">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">Try asking</p>
          <div className="flex flex-wrap gap-2 pb-3">
            {EXAMPLE_QUESTIONS.map((question) => (
              <button
                key={question}
                type="button"
                onClick={() => handleExampleClick(question)}
                disabled={state.status !== 'connected'}
                className="text-xs bg-background text-gray-300 px-3 py-1.5 rounded-full border border-gray-700 hover:border-accent hover:text-ink disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-accent transition-colors"
              >
                {question}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Input area */}
      <div className="border-t border-gray-800 bg-surface px-4 py-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={inputText}
            onChange={(event) => setInputText(event.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder="Type a message..."
            aria-label="Message input"
            rows={1}
            className="flex-1 resize-none max-h-32 bg-background text-gray-200 placeholder:text-gray-500 rounded-2xl px-4 py-2.5 border border-gray-800 focus:outline-none focus:ring-2 focus:ring-accent"
          />
          <button
            type="button"
            onClick={handleSend}
            disabled={!canSend}
            aria-label="Send message"
            className="min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full bg-accent text-white disabled:bg-gray-700 disabled:text-gray-500 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background transition-colors"
          >
            <Send size={18} />
          </button>
        </div>
      </div>
    </div>
  );

  // Render as portal so it sits above everything including BottomNav
  return createPortal(panel, document.body);
}

/**
 * Custom renderers so markdown from the assistant (bold text, headings,
 * GFM tables, lists, horizontal rules) matches the app's dark theme
 * instead of relying on default browser styling. GFM table support comes
 * from remark-gfm, since tool responses (e.g. work order breakdowns) are
 * frequently rendered as pipe tables rather than plain sentences.
 */
const MARKDOWN_COMPONENTS: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0 leading-snug">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  h1: ({ children }) => <h3 className="font-semibold text-ink mt-2 mb-1">{children}</h3>,
  h2: ({ children }) => <h3 className="font-semibold text-ink mt-2 mb-1">{children}</h3>,
  h3: ({ children }) => <h3 className="font-semibold text-ink mt-2 mb-1">{children}</h3>,
  ul: ({ children }) => <ul className="list-disc pl-5 mb-2 space-y-0.5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 mb-2 space-y-0.5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="leading-snug">{children}</li>,
  hr: () => <hr className="border-gray-700 my-2" />,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent underline">
      {children}
    </a>
  ),
  code: ({ children }) => (
    <code className="bg-background/60 px-1 py-0.5 rounded text-xs">{children}</code>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto mb-2 last:mb-0 -mx-1">
      <table className="min-w-full text-xs border-collapse">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="border-b border-gray-700">{children}</thead>,
  th: ({ children }) => (
    <th className="text-left font-medium text-gray-400 px-2 py-1 whitespace-nowrap">{children}</th>
  ),
  td: ({ children }) => (
    <td className="px-2 py-1 border-t border-gray-800 align-top">{children}</td>
  ),
};

/**
 * Props for the MessageBubble subcomponent rendering a single chat
 * message aligned according to its role.
 */
interface MessageBubbleProps {
  message: ChatMessage;
}

/**
 * Renders a single message bubble, right-aligned for the user and
 * left-aligned for the assistant, with a small timestamp label.
 *
 * The assistant's text is rendered as Markdown (GFM tables, bold,
 * headings, lists) rather than as a raw string, since tool-backed answers
 * (e.g. work order breakdowns) frequently arrive formatted as pipe tables.
 * The user's own message is rendered as plain text - it is exactly what
 * they typed, so there is nothing to interpret as Markdown.
 */
function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const time = message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  return (
    <div className={isUser ? 'text-right' : 'text-left'}>
      <div
        className={
          isUser
            ? 'inline-block bg-accent/10 text-gray-200 text-sm rounded-2xl rounded-br-sm px-4 py-2 max-w-[85%] text-left'
            : 'inline-block bg-surface text-gray-200 text-sm rounded-2xl rounded-bl-sm px-4 py-2 max-w-[92%] text-left'
        }
      >
        {isUser ? (
          message.text
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
            {message.text}
          </ReactMarkdown>
        )}
      </div>
      <span className="block text-xs text-gray-500 mt-1">{time}</span>
    </div>
  );
}

/**
 * Three animated dots indicating that the agent is composing a response.
 * Uses CSS keyframe animation with staggered delays for a bouncing effect.
 */
function TypingDots() {
  return (
    <div className="flex items-center gap-1" aria-hidden="true">
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          className="w-2 h-2 rounded-full bg-gray-400"
          style={{
            animation: 'chatPanelTypingDot 1.4s ease-in-out infinite',
            animationDelay: `${index * 0.2}s`,
          }}
        />
      ))}
      {/* Keyframe animation for typing dots — uses a globally-unique name to avoid collisions */}
      <style dangerouslySetInnerHTML={{ __html: `
        @keyframes chatPanelTypingDot {
          0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
          30% { transform: translateY(-4px); opacity: 1; }
        }
      ` }} />
    </div>
  );
}
