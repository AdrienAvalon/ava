import { afterEach, describe, expect, it, vi } from 'vitest';
import { OIDC_AUTHORITY, OIDC_CLIENT_ID } from '../auth/oidcIdentity';
import {
  effacerConversation,
  entetesIdentite,
  jetonOidc,
  lireConversation,
} from './memoireServeur';

function storage(values: Record<string, string>): Storage {
  const keys = Object.keys(values);
  return {
    get length() { return keys.length; },
    clear: () => undefined,
    getItem: (key: string) => values[key] ?? null,
    key: (index: number) => keys[index] ?? null,
    removeItem: () => undefined,
    setItem: () => undefined,
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('entetesIdentite', () => {
  it('transmet le jeton OIDC au chat sans le transformer en persona', () => {
    const exacte = `oidc.user:${OIDC_AUTHORITY}:${OIDC_CLIENT_ID}`;
    vi.stubGlobal('sessionStorage', storage({
      'oidc.user:https://stale.invalid:other-client': JSON.stringify({ id_token: 'stale.jwt.value' }),
      [exacte]: JSON.stringify({ id_token: 'signed.jwt.value' }),
    }));
    expect(jetonOidc()).toBe('signed.jwt.value');
    expect(entetesIdentite({ 'Content-Type': 'application/json' })).toEqual({
      'Content-Type': 'application/json',
      'X-Ava-Identity': 'signed.jwt.value',
    });
  });

  it('ne fabrique aucun principal sans jeton', () => {
    vi.stubGlobal('sessionStorage', storage({}));
    expect(entetesIdentite()).toEqual({});
  });

  it('préserve le cache local quand le GET refuse un jeton forgé ou expiré', async () => {
    const exacte = `oidc.user:${OIDC_AUTHORITY}:${OIDC_CLIENT_ID}`;
    vi.stubGlobal('sessionStorage', storage({
      [exacte]: JSON.stringify({ id_token: 'forged.jwt.value' }),
    }));
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 401 });
    vi.stubGlobal('fetch', fetchMock);

    await expect(lireConversation()).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledWith('/v1/ava/conversation', expect.objectContaining({
      headers: { 'X-Ava-Identity': 'forged.jwt.value' },
      signal: expect.any(AbortSignal),
    }));
  });

  it.each([401, 503])(
    'refuse d\u2019acquitter un DELETE rejeté avec HTTP %i',
    async (status) => {
      vi.stubGlobal('sessionStorage', storage({}));
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status }));

      await expect(effacerConversation()).resolves.toBe(false);
    },
  );

  it('acquitte le DELETE uniquement après un succès HTTP', async () => {
    vi.stubGlobal('sessionStorage', storage({}));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }));

    await expect(effacerConversation()).resolves.toBe(true);
  });

  it('refuse aussi l\u2019acquittement quand le DELETE n\u2019atteint pas le serveur', async () => {
    vi.stubGlobal('sessionStorage', storage({}));
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    await expect(effacerConversation()).resolves.toBe(false);
  });
});
