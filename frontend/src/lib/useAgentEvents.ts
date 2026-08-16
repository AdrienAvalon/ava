import { useEffect, useRef } from 'react';
import { jetonOidc } from '../components/immersive/memoireServeur';
import { getApiKey, getBase } from './api';

export interface AgentEvent {
  type: string;
  timestamp: number;
  data: Record<string, unknown>;
}

export const AGENT_EVENTS_SUBPROTOCOL = 'ava-agent-events-v1';
const OIDC_SUBPROTOCOL_PREFIX = 'ava-oidc-v1.';
const API_KEY_SUBPROTOCOL_PREFIX = 'ava-api-key-v1.';

function encodeProtocolCredential(value: string): string {
  // Protocol-token encoding is not encryption. It keeps credentials out of
  // request URLs and lets the server echo only the fixed protocol marker.
  const bytes = new TextEncoder().encode(value);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

export function buildAgentEventsProtocols(
  oidcToken: string,
  apiKey = '',
): string[] {
  const protocols = [
    AGENT_EVENTS_SUBPROTOCOL,
    `${OIDC_SUBPROTOCOL_PREFIX}${encodeProtocolCredential(oidcToken)}`,
  ];
  if (apiKey) {
    protocols.push(
      `${API_KEY_SUBPROTOCOL_PREFIX}${encodeProtocolCredential(apiKey)}`,
    );
  }
  return protocols;
}

export function buildAgentEventsWsUrl(agentId: string): string {
  const base = getBase();
  let origin: string;
  if (base) {
    origin = base.replace(/^http/, 'ws').replace(/\/+$/, '');
  } else {
    const loc = window.location;
    origin = `${loc.protocol === 'https:' ? 'wss:' : 'ws:'}//${loc.host}`;
  }
  return `${origin}/v1/agents/events?agent_id=${encodeURIComponent(agentId)}`;
}

export function openAgentEventsSocket(agentId: string): WebSocket | null {
  const oidcToken = jetonOidc();
  if (!oidcToken) return null;
  return new WebSocket(
    buildAgentEventsWsUrl(agentId),
    buildAgentEventsProtocols(oidcToken, getApiKey()),
  );
}

/**
 * Subscribe to agent events over WebSocket.
 * Auto-reconnects with backoff when the socket drops.
 */
export function useAgentEvents(
  agentId: string | undefined,
  onEvent: (event: AgentEvent) => void,
  eventTypes?: readonly string[],
): void {
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const typesRef = useRef(eventTypes);
  typesRef.current = eventTypes;

  useEffect(() => {
    if (!agentId) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (closed) return;
      try {
        ws = openAgentEventsSocket(agentId);
        if (!ws) {
          schedule();
          return;
        }
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => {
        retry = 0;
      };
      ws.onmessage = (msg) => {
        try {
          const payload = JSON.parse(msg.data) as AgentEvent;
          const allowed = typesRef.current;
          if (allowed && !allowed.includes(payload.type)) return;
          onEventRef.current(payload);
        } catch {
          // ignore malformed payload
        }
      };
      ws.onclose = () => {
        if (!closed) schedule();
      };
      ws.onerror = () => {
        ws?.close();
      };
    };

    const schedule = () => {
      if (closed) return;
      const delay = Math.min(30000, 1000 * 2 ** Math.min(retry, 5));
      retry += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [agentId]);
}
