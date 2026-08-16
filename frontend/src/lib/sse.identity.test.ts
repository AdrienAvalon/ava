import { afterEach, describe, expect, it, vi } from 'vitest';
import { streamChat } from './sse';

afterEach(() => vi.unstubAllGlobals());

describe('streamChat generic desktop path', () => {
  it('n’envoie pas X-Ava-Identity tant que son historique reste global', async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
          + 'data: [DONE]\n\n',
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const generator = streamChat({
      model: 'test-model',
      messages: [{ role: 'user', content: 'hello' }],
      stream: true,
    });
    await generator.next();

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers['X-Ava-Identity']).toBeUndefined();
  });

  it('refuse une fin de flux sans marqueur terminal', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    )));
    const generator = streamChat({
      model: 'test-model',
      messages: [{ role: 'user', content: 'hello' }],
      stream: true,
    });

    await expect((async () => {
      for await (const _event of generator) { /* consume */ }
    })()).rejects.toThrow('terminal sentinel');
  });

  it('refuse une erreur structuree sans la consacrer en contenu', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      'data: {"error":{"message":"Chat generation failed"}}\n\n'
        + 'data: [DONE]\n\n',
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    )));
    const generator = streamChat({
      model: 'test-model',
      messages: [{ role: 'user', content: 'hello' }],
      stream: true,
    });

    await expect(generator.next()).rejects.toThrow('Chat generation failed');
  });
});
