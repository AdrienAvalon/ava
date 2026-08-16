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
          try {
            const parsed = JSON.parse(data) as {
              error?: { message?: unknown };
              choices?: Array<{ finish_reason?: unknown }>;
            };
            if (parsed.error) {
              throw new Error(
                typeof parsed.error.message === 'string'
                  ? parsed.error.message
                  : 'Chat generation failed',
              );
            }
            const finishReason = parsed.choices?.[0]?.finish_reason;
            if (finishReason === 'stop' || finishReason === 'tool_calls') {
              sawTerminal = true;
            } else if (finishReason != null) {
              throw new Error('Chat stream ended without a complete response');
            }
          } catch (error) {
            if (error instanceof SyntaxError) {
              // Preserve extension events that are intentionally not JSON.
            } else {
              throw error;
            }
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
        if (data === '[DONE]') return;
        try {
          const parsed = JSON.parse(data) as ResearchEvent;
          yield parsed;
          if (parsed.type === 'done') return;
        } catch {
          // skip malformed chunks
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
