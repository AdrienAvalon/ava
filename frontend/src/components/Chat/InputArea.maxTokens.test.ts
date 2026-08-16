import { afterEach, describe, expect, it, vi } from 'vitest';

const SETTINGS_KEY = 'openjarvis-settings';

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  constructor(initial: Record<string, string> = {}) {
    for (const [key, value] of Object.entries(initial)) this.values.set(key, value);
  }

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return Array.from(this.values.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

async function loadFrontend(initialSettings?: Record<string, unknown>) {
  vi.resetModules();
  const storage = new MemoryStorage(
    initialSettings
      ? { [SETTINGS_KEY]: JSON.stringify(initialSettings) }
      : {},
  );
  vi.stubGlobal('localStorage', storage);
  vi.stubGlobal('crypto', { randomUUID: () => '00000000-0000-4000-8000-000000000001' });

  const storeModule = await import('../../lib/store');
  const inputAreaModule = await import('./InputArea');
  return {
    storage,
    store: storeModule.useAppStore,
    buildChatRequest: inputAreaModule.buildChatRequest,
  };
}

function requestFor(
  buildChatRequest: Awaited<ReturnType<typeof loadFrontend>>['buildChatRequest'],
  settings: {
    temperature: number;
    maxTokens: number;
    maxTokensSource: 'server' | 'user';
  },
) {
  return buildChatRequest(
    'claude-test',
    [{ role: 'user', content: 'Bonjour' }],
    settings.temperature,
    settings.maxTokens,
    settings.maxTokensSource,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('generic chat max token limit', () => {
  it('omits max_tokens for a fresh store so the server owns the limit', async () => {
    const { store, buildChatRequest } = await loadFrontend();

    expect(store.getState().settings.maxTokensSource).toBe('server');
    expect(requestFor(buildChatRequest, store.getState().settings)).not.toHaveProperty('max_tokens');
  });

  it('sends a visible user override and persists its provenance', async () => {
    const { storage, store, buildChatRequest } = await loadFrontend();

    store.getState().setMaxTokensOverride(8192);

    expect(requestFor(buildChatRequest, store.getState().settings)).toMatchObject({
      max_tokens: 8192,
    });
    expect(JSON.parse(storage.getItem(SETTINGS_KEY)!)).toMatchObject({
      settingsVersion: 2,
      maxTokens: 8192,
      maxTokensSource: 'user',
    });

    vi.resetModules();
    const { useAppStore: reloadedStore } = await import('../../lib/store');
    const { buildChatRequest: reloadedBuilder } = await import('./InputArea');
    expect(reloadedStore.getState().settings).toMatchObject({
      maxTokens: 8192,
      maxTokensSource: 'user',
    });
    expect(requestFor(reloadedBuilder, reloadedStore.getState().settings)).toMatchObject({
      max_tokens: 8192,
    });
  });

  it('returns to the server limit and keeps that reset after reload', async () => {
    const { storage, store, buildChatRequest } = await loadFrontend({
      settingsVersion: 2,
      maxTokens: 12288,
      maxTokensSource: 'user',
    });

    store.getState().setMaxTokensOverride(null);
    expect(requestFor(buildChatRequest, store.getState().settings)).not.toHaveProperty('max_tokens');
    expect(JSON.parse(storage.getItem(SETTINGS_KEY)!)).toMatchObject({
      settingsVersion: 2,
      maxTokens: 12288,
      maxTokensSource: 'server',
    });

    vi.resetModules();
    const { useAppStore: reloadedStore } = await import('../../lib/store');
    const { buildChatRequest: reloadedBuilder } = await import('./InputArea');
    expect(reloadedStore.getState().settings.maxTokensSource).toBe('server');
    expect(requestFor(reloadedBuilder, reloadedStore.getState().settings)).not.toHaveProperty('max_tokens');
  });

  it('migrates the legacy implicit 4096 default to the server limit', async () => {
    const { storage, store, buildChatRequest } = await loadFrontend({
      theme: 'dark',
      maxTokens: 4096,
    });

    expect(store.getState().settings.maxTokensSource).toBe('server');
    expect(requestFor(buildChatRequest, store.getState().settings)).not.toHaveProperty('max_tokens');
    expect(JSON.parse(storage.getItem(SETTINGS_KEY)!)).toMatchObject({
      settingsVersion: 2,
      maxTokens: 4096,
      maxTokensSource: 'server',
    });

    store.getState().updateSettings({ temperature: 0.4 });
    expect(JSON.parse(storage.getItem(SETTINGS_KEY)!)).toMatchObject({
      settingsVersion: 2,
      maxTokens: 4096,
      maxTokensSource: 'server',
      temperature: 0.4,
    });
  });

  it('preserves a distinct legacy maxTokens value as a user override', async () => {
    const { storage, store, buildChatRequest } = await loadFrontend({
      theme: 'dark',
      maxTokens: 8192,
    });

    expect(store.getState().settings.maxTokensSource).toBe('user');
    expect(requestFor(buildChatRequest, store.getState().settings)).toMatchObject({
      max_tokens: 8192,
    });
    expect(JSON.parse(storage.getItem(SETTINGS_KEY)!)).toMatchObject({
      settingsVersion: 2,
      maxTokens: 8192,
      maxTokensSource: 'user',
    });
  });

  it('migrates legacy settings without maxTokens to the server limit deterministically', async () => {
    const { storage, store, buildChatRequest } = await loadFrontend({ theme: 'dark' });

    expect(store.getState().settings.maxTokensSource).toBe('server');
    expect(requestFor(buildChatRequest, store.getState().settings)).not.toHaveProperty('max_tokens');
    expect(JSON.parse(storage.getItem(SETTINGS_KEY)!)).toMatchObject({
      settingsVersion: 2,
      maxTokens: 4096,
      maxTokensSource: 'server',
    });

    store.getState().updateSettings({ temperature: 0.4 });
    expect(JSON.parse(storage.getItem(SETTINGS_KEY)!)).toMatchObject({
      settingsVersion: 2,
      maxTokens: 4096,
      maxTokensSource: 'server',
      temperature: 0.4,
    });
  });
});
