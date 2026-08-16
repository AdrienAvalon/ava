import type { ResearchEvent, SSEEvent } from '../types';
import { getBase, authHeaders } from './api';

export interface ChatRequest {
  model: string;
  messages: Array<{ role: string; content: string }>;
  stream: true;
  temperature?: number;
  max_tokens?: number;
}

export async function* streamChat(
  request: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const base = getBase();
  // This generic desktop `/chat` view persists a single global conversation
  // in localStorage. Until that store is namespaced by a verified principal,
  // it deliberately sends only the daemon API key: no X-Ava-Identity means no
  // private relationship overlay can be selected for this path.
  const response = await fetch(`${base}/v1/chat/completions`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Chat request failed: ${response.status}`);
  }

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let currentEvent: string | undefined;
  let sawTerminal = false;
  let sawDone = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const data = line.slice(6);
          if (data === '[DONE]') {
            sawDone = true;
            if (!sawTerminal) {
              throw new Error('Chat stream ended without a complete response');
            }
            return;
          }
          let parsed: {
            error?: { message?: unknown };
            choices?: Array<{ finish_reason?: unknown }>;
          };
          try {
            parsed = JSON.parse(data) as typeof parsed;
          } catch {
            throw new Error('Chat stream contained malformed data');
          }
          if (parsed.error) {
            throw new Error(
              typeof parsed.error.message === 'string'
                ? parsed.error.message
                : 'Chat generation failed',
            );
          }
          const finishReason = parsed.choices?.[0]?.finish_reason;
          if (finishReason === 'stop') {
            sawTerminal = true;
          } else if (finishReason != null) {
            throw new Error('Chat stream ended without a complete response');
          }
          yield { event: currentEvent, data };
          currentEvent = undefined;
        } else if (line.trim() === '') {
          currentEvent = undefined;
        }
      }
    }
    if (!sawDone) {
      throw new Error('Chat stream ended without the terminal sentinel');
    }
  } finally {
    reader.releaseLock();
  }
}

export async function* streamResearch(
  query: string,
  model?: string,
  signal?: AbortSignal,
): AsyncGenerator<ResearchEvent> {
  // /api/research is mounted at the server root — strip any trailing /v1
  // from the base so configurations like "http://host:8000/v1" still resolve.
  const base = getBase().replace(/\/v1\/?$/, '');
  const response = await fetch(`${base}/api/research`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ query, ...(model ? { model } : {}) }),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Research request failed: ${response.status}`);
  }

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sawSynthesis = false;

  const parseEvent = (data: string): ResearchEvent => {
    if (data === '[DONE]') {
      throw new Error('Research stream used a legacy terminal sentinel');
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      throw new Error('Research stream returned malformed data');
    }
    if (!parsed || typeof parsed !== 'object' || !('type' in parsed)) {
      throw new Error('Research stream returned an invalid event');
    }

    const event = parsed as ResearchEvent;
    if (event.type === 'error') {
      throw new Error(event.message || 'Research failed before completion');
    }
    if (event.type === 'synthesis') {
      if (typeof event.text !== 'string') {
        throw new Error('Research stream returned an invalid synthesis chunk');
      }
      sawSynthesis ||= event.text.trim().length > 0;
    }
    return event;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        const event = parseEvent(data);
        if (event.type === 'done') {
          if (event.status !== 'success') {
            throw new Error('Research failed before completion');
          }
          if (!sawSynthesis) {
            throw new Error('Research stream ended without a complete synthesis');
          }
          yield event;
          return;
        }
        yield event;
      }
    }

    throw new Error('Research stream ended without an explicit done event');
  } finally {
    reader.releaseLock();
  }
}
