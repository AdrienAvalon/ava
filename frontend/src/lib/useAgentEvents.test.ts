import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { jetonOidc } from '../components/immersive/memoireServeur';
import { getApiKey, getBase } from './api';
import {
  AGENT_EVENTS_SUBPROTOCOL,
  buildAgentEventsProtocols,
  buildAgentEventsWsUrl,
  openAgentEventsSocket,
} from './useAgentEvents';

vi.mock('./api', () => ({
  getApiKey: vi.fn(),
  getBase: vi.fn(),
}));

vi.mock('../components/immersive/memoireServeur', () => ({
  jetonOidc: vi.fn(),
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    FakeWebSocket.instances.push(this);
  }

  close(): void {}
}

function decodeCredential(protocol: string, prefix: string): string {
  const encoded = protocol
    .slice(prefix.length)
    .replace(/-/g, '+')
    .replace(/_/g, '/');
  const binary = atob(encoded + '='.repeat((4 - (encoded.length % 4)) % 4));
  return new TextDecoder().decode(
    Uint8Array.from(binary, (char) => char.charCodeAt(0)),
  );
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.mocked(getBase).mockReturnValue('https://ava.example.test/');
  vi.mocked(getApiKey).mockReturnValue('daemon-secret');
  vi.mocked(jetonOidc).mockReturnValue('signed.oidc.token');
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('managed-agent event WebSocket identity', () => {
  it('keeps credentials out of the URL and sends dedicated subprotocols', () => {
    const socket = openAgentEventsSocket('agent/one');

    expect(socket).toBeInstanceOf(FakeWebSocket);
    const created = FakeWebSocket.instances[0];
    expect(created.url).toBe(
      'wss://ava.example.test/v1/agents/events?agent_id=agent%2Fone',
    );
    expect(created.url).not.toContain('signed.oidc.token');
    expect(created.url).not.toContain('daemon-secret');
    expect(created.protocols).toEqual(
      buildAgentEventsProtocols('signed.oidc.token', 'daemon-secret'),
    );

    const protocols = created.protocols as string[];
    expect(protocols[0]).toBe(AGENT_EVENTS_SUBPROTOCOL);
    expect(protocols.join(',')).not.toContain('signed.oidc.token');
    expect(protocols.join(',')).not.toContain('daemon-secret');
    expect(decodeCredential(protocols[1], 'ava-oidc-v1.')).toBe(
      'signed.oidc.token',
    );
    expect(decodeCredential(protocols[2], 'ava-api-key-v1.')).toBe(
      'daemon-secret',
    );
  });

  it('does not create an anonymous managed-agent socket', () => {
    vi.mocked(jetonOidc).mockReturnValue(null);

    expect(openAgentEventsSocket('agent-one')).toBeNull();
    expect(FakeWebSocket.instances).toEqual([]);
  });

  it('omits only the optional daemon-key protocol when no key is configured', () => {
    vi.mocked(getApiKey).mockReturnValue('');

    openAgentEventsSocket('agent-one');

    expect(FakeWebSocket.instances[0].protocols).toEqual([
      AGENT_EVENTS_SUBPROTOCOL,
      buildAgentEventsProtocols('signed.oidc.token')[1],
    ]);
  });

  it('requires an agent id in every URL it builds', () => {
    expect(buildAgentEventsWsUrl('agent-one')).toBe(
      'wss://ava.example.test/v1/agents/events?agent_id=agent-one',
    );
  });
});
