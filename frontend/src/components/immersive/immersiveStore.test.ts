import { afterEach, describe, expect, it, vi } from 'vitest';
import { OIDC_AUTHORITY, OIDC_CLIENT_ID } from '../auth/oidcIdentity';

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

function token(subject: string): string {
  const payload = btoa(JSON.stringify({ sub: subject }))
    .replace(/=/g, '')
    .replace(/\+/g, '-')
    .replace(/\//g, '_');
  return `header.${payload}.signature`;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe('cleStockage — namespace local exact', () => {
  it('ignore une session OIDC stale même si elle apparaît avant la session Ava', async () => {
    const exactKey = `oidc.user:${OIDC_AUTHORITY}:${OIDC_CLIENT_ID}`;
    vi.stubGlobal('sessionStorage', storage({
      'oidc.user:https://stale.invalid:other': JSON.stringify({
        id_token: token('stale-subject'),
      }),
      [exactKey]: JSON.stringify({ id_token: token('verified-subject') }),
    }));
    vi.stubGlobal('localStorage', storage({}));

    const { cleStockage } = await import('./immersiveStore');
    expect(cleStockage()).toBe('ava.transcript.v2:verified-subject');
  });

  it('ne crée aucun cache depuis une session d’un autre client', async () => {
    vi.stubGlobal('sessionStorage', storage({
      'oidc.user:https://stale.invalid:other': JSON.stringify({
        id_token: token('stale-subject'),
      }),
    }));
    vi.stubGlobal('localStorage', storage({}));

    const { cleStockage } = await import('./immersiveStore');
    expect(cleStockage()).toBeNull();
  });

  it.each([
    ['a', 3],
    ['abc', 2],
  ])(
    'décode un payload base64url sans padding (%s, reste %i)',
    async (subject, expectedRemainder) => {
      const exactKey = `oidc.user:${OIDC_AUTHORITY}:${OIDC_CLIENT_ID}`;
      const idToken = token(subject);
      expect(idToken.split('.')[1].length % 4).toBe(expectedRemainder);
      vi.stubGlobal('sessionStorage', storage({
        [exactKey]: JSON.stringify({ id_token: idToken }),
      }));
      vi.stubGlobal('localStorage', storage({}));

      const { cleStockage } = await import('./immersiveStore');
      expect(cleStockage()).toBe(`ava.transcript.v2:${subject}`);
    },
  );
});

describe('chargerHistoriqueModele — rechargement transactionnel', () => {
  it('écarte les questions échouées ou interrompues et ne rejoue que les paires', async () => {
    const subject = 'reload-subject';
    const exactKey = `oidc.user:${OIDC_AUTHORITY}:${OIDC_CLIENT_ID}`;
    const transcriptKey = `ava.transcript.v2:${subject}`;
    vi.stubGlobal('sessionStorage', storage({
      [exactKey]: JSON.stringify({ id_token: token(subject) }),
    }));
    vi.stubGlobal('localStorage', storage({
      [transcriptKey]: JSON.stringify([
        { id: 1, role: 'user', text: 'question échouée', at: '10:00' },
        { id: 2, role: 'system', text: 'Erreur HTTP', at: '10:00' },
        { id: 3, role: 'user', text: 'question validée', at: '10:01' },
        { id: 4, role: 'ava', text: 'réponse validée', at: '10:01' },
        { id: 5, role: 'user', text: 'question interrompue', at: '10:02' },
      ]),
    }));

    const { chargerHistoriqueModele } = await import('./immersiveStore');
    expect(chargerHistoriqueModele()).toEqual([
      { role: 'user', content: 'question validée' },
      { role: 'assistant', content: 'réponse validée' },
    ]);
  });
});
