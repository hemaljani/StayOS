// Exercise the actual AppSync protocol client, including authorization failures.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RealtimeClient, type RealtimeHandlers } from '@/lib/realtime';

vi.mock('@/lib/auth', () => ({ getIdToken: () => 'test-id-token' }));
vi.mock('@/lib/api', () => ({ authFetch: vi.fn() }));

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror = null;
  close = vi.fn(() => this.onclose?.());
  constructor(_url: string, _protocols: string[]) { Socket.instances.push(this); }
  send(message: string) { this.sent.push(JSON.parse(message)); }
  receive(message: unknown) { this.onmessage?.({ data: JSON.stringify(message) } as MessageEvent); }
}

describe('AppSync subscription health and property isolation', () => {
  let client: RealtimeClient;
  let socket: Socket;
  let handlers: RealtimeHandlers;
  beforeEach(() => {
    vi.useFakeTimers();
    Socket.instances = [];
    vi.stubGlobal('WebSocket', Socket);
    handlers = {
      onBroadcastEvent: vi.fn<RealtimeHandlers['onBroadcastEvent']>(),
      onUnicastEvent: vi.fn<RealtimeHandlers['onUnicastEvent']>(),
      onResubscribe: vi.fn<RealtimeHandlers['onResubscribe']>(),
      onConnectionChange: vi.fn<NonNullable<RealtimeHandlers['onConnectionChange']>>(),
    };
    client = new RealtimeClient(
      { httpEndpoint: 'https://example.com', wssEndpoint: 'wss://example.com/event/realtime', namespace: 'pulse' },
      'P1', 'gm1', handlers
    );
    client.start();
    socket = Socket.instances[0];
    socket.receive({ type: 'connection_ack' });
  });
  afterEach(() => { client.stop(); vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('reports live only after the broadcast subscription succeeds', () => {
    expect(handlers.onConnectionChange).not.toHaveBeenCalledWith(true);
    expect(socket.sent.map((message) => message.channel)).toEqual(['/pulse/alerts/P1', '/pulse/alerts/P1/gm1']);
    socket.receive({ type: 'subscribe_success', id: socket.sent[0].id });
    socket.receive({ type: 'subscribe_success', id: socket.sent[1].id });
    expect(handlers.onConnectionChange).toHaveBeenCalledWith(true);
    expect(handlers.onResubscribe).toHaveBeenCalledOnce();
    client.start();
    expect(Socket.instances).toHaveLength(1);
  });

  it('forces reconnect after an operation error even if the socket stays open', () => {
    socket.receive({ type: 'subscribe_error', id: socket.sent[0].id });
    expect(socket.close).toHaveBeenCalledOnce();
    expect(handlers.onConnectionChange).toHaveBeenCalledWith(false);
    vi.advanceTimersByTime(1500);
    expect(Socket.instances).toHaveLength(2);
  });

  it('routes only recognized events for the subscribed property', () => {
    const id = socket.sent[0].id;
    socket.receive({ type: 'data', id, event: [JSON.stringify({ eventType: 'ALERT_CREATED', propertyId: 'P2', alertId: 'foreign' })] });
    socket.receive({ type: 'data', id, event: [JSON.stringify({ eventType: 'INVALID', propertyId: 'P1', alertId: 'bad' })] });
    expect(handlers.onBroadcastEvent).not.toHaveBeenCalled();
    socket.receive({ type: 'data', id, event: [JSON.stringify({ eventType: 'ALERT_CREATED', propertyId: 'P1', alertId: 'own' })] });
    expect(handlers.onBroadcastEvent).toHaveBeenCalledOnce();
  });
});
