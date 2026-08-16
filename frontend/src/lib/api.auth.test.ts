import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { OIDC_AUTHORITY, OIDC_CLIENT_ID } from '../components/auth/oidcIdentity';

// Regression for #266: the frontend must send the local API key as a Bearer
// token on /v1 + /api requests, or `jarvis serve` with a key configured 401s
// every data-plane call. These tests cover the pure helpers (getApiKey,
// authHeaders) that source the key and build the header.

const SETTINGS_KEY = 'openjarvis-settings';
const fetchMock = vi.fn<typeof fetch>();

// Minimal in-memory localStorage stub so the helpers can run under node
// (no jsdom dependency).
class MemoryStorage {
  private store = new Map<string, string>();
  getItem(k: string): string | null {
    return this.store.has(k) ? (this.store.get(k) as string) : null;
  }
  setItem(k: string, v: string): void {
    this.store.set(k, String(v));
  }
  removeItem(k: string): void {
    this.store.delete(k);
  }
  clear(): void {
    this.store.clear();
  }
}

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'test-anon-key');
  fetchMock.mockReset();
  globalThis.fetch = fetchMock;
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
  (globalThis as unknown as { sessionStorage: MemoryStorage }).sessionStorage =
    new MemoryStorage();
});

afterEach(() => {
  vi.unstubAllEnvs();
  (globalThis as unknown as { localStorage?: MemoryStorage }).localStorage =
    undefined;
  (globalThis as unknown as { sessionStorage?: MemoryStorage }).sessionStorage =
    undefined;
});

async function freshApi() {
  // Re-import to pick up the current localStorage stub.
  return await import('./api');
}

describe('getApiKey', () => {
  it('returns empty string when no key is configured', async () => {
    const { getApiKey } = await freshApi();
    expect(getApiKey()).toBe('');
  });

  it('reads apiKey from the openjarvis-settings localStorage blob', async () => {
    localStorage.setItem(
      SETTINGS_KEY,
      JSON.stringify({ apiUrl: 'http://x', apiKey: 'sk-local-123' }),
    );
    const { getApiKey } = await freshApi();
    expect(getApiKey()).toBe('sk-local-123');
  });

  it('returns empty string when the blob has no apiKey field', async () => {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ apiUrl: 'http://x' }));
    const { getApiKey } = await freshApi();
    expect(getApiKey()).toBe('');
  });
});

describe('authHeaders', () => {
  it('omits Authorization when no key is set (keyless default unchanged)', async () => {
    const { authHeaders } = await freshApi();
    expect(authHeaders()).toEqual({});
  });

  it('adds a Bearer Authorization header when a key is set', async () => {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ apiKey: 'sk-local-123' }));
    const { authHeaders } = await freshApi();
    expect(authHeaders()).toEqual({ Authorization: 'Bearer sk-local-123' });
  });

  it('merges extra headers alongside Authorization', async () => {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ apiKey: 'sk-local-123' }));
    const { authHeaders } = await freshApi();
    expect(authHeaders({ 'Content-Type': 'application/json' })).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'Bearer sk-local-123',
    });
  });
});

describe('managed agent identity', () => {
  it('sends both the daemon key and the exact verified OIDC token', async () => {
    const oidcKey = `oidc.user:${OIDC_AUTHORITY}:${OIDC_CLIENT_ID}`;
    sessionStorage.setItem(
      oidcKey,
      JSON.stringify({ id_token: 'signed.managed-agent.jwt' }),
    );
    localStorage.setItem(
      SETTINGS_KEY,
      JSON.stringify({ apiKey: 'sk-local-123' }),
    );
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ agents: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const { fetchManagedAgents } = await freshApi();

    await expect(fetchManagedAgents()).resolves.toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith('/v1/managed-agents', {
      headers: {
        Authorization: 'Bearer sk-local-123',
        'X-Ava-Identity': 'signed.managed-agent.jwt',
      },
    });
  });
});

describe('tool credentials', () => {
  it('reads credential status from the local server', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ TAVILY_API_KEY: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const { fetchToolCredentialStatus } = await freshApi();

    await expect(fetchToolCredentialStatus('web_search')).resolves.toEqual({
      TAVILY_API_KEY: true,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/tools/web_search/credentials/status',
      { headers: {} },
    );
  });

  it('saves a tool credential through the local server', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }));
    const { saveToolCredentials } = await freshApi();

    await saveToolCredentials('web_search', {
      TAVILY_API_KEY: 'tvly-test',
    });

    expect(fetchMock).toHaveBeenCalledWith('/v1/tools/web_search/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ TAVILY_API_KEY: 'tvly-test' }),
    });
  });

  it('deletes a tool credential through the local server', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }));
    const { deleteToolCredential } = await freshApi();

    await deleteToolCredential('web_search', 'TAVILY_API_KEY');

    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/tools/web_search/credentials/TAVILY_API_KEY',
      { method: 'DELETE', headers: {} },
    );
  });
});

describe('managed agent streaming errors', () => {
  it('rejects the promise and never reports a failed turn as delivered', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        'data: {"error":{"type":"generation_error","message":"Managed agent generation failed"}}\n\n'
          + 'data: [DONE]\n\n',
        {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        },
      ),
    );
    const onDone = vi.fn();
    const { sendAgentMessage } = await freshApi();

    await expect(
      sendAgentMessage('agent-1', 'question', 'immediate', { onDone }),
    ).rejects.toThrow('Managed agent generation failed');
    expect(onDone).not.toHaveBeenCalled();
  });

  it('rejects an EOF without the terminal sentinel', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
        {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        },
      ),
    );
    const onDone = vi.fn();
    const { sendAgentMessage } = await freshApi();

    await expect(
      sendAgentMessage('agent-1', 'question', 'immediate', { onDone }),
    ).rejects.toThrow('without a complete response');
    expect(onDone).not.toHaveBeenCalled();
  });

  it('rejects a non-stop terminal reason', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
          + 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
          + 'data: [DONE]\n\n',
        {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        },
      ),
    );
    const onDone = vi.fn();
    const { sendAgentMessage } = await freshApi();

    await expect(
      sendAgentMessage('agent-1', 'question', 'immediate', { onDone }),
    ).rejects.toThrow('without a complete response');
    expect(onDone).not.toHaveBeenCalled();
  });
});
