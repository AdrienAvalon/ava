import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ResearchEvent } from '../types';
import { streamResearch } from './sse';

const encoder = new TextEncoder();

function researchResponse(chunks: string[]): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  );
}

async function collectResearchEvents(): Promise<ResearchEvent[]> {
  const events: ResearchEvent[] = [];
  for await (const event of streamResearch('question de test')) {
    events.push(event);
  }
  return events;
}

afterEach(() => vi.unstubAllGlobals());

describe('streamResearch terminal contract', () => {
  it('refuse des fragments suivis d’une fin de flux sans done', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => researchResponse([
      'data: {"type":"synth',
      'esis","text":"Réponse partielle"}\n\n',
    ])));

    await expect(collectResearchEvents()).rejects.toThrow(
      'without an explicit done event',
    );
  });

  it('refuse un terminal done avec le statut error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => researchResponse([
      'data: {"type":"synthesis","text":"Fragment"}\n\n',
      'data: {"type":"done","status":"error"}\n\n',
    ])));

    await expect(collectResearchEvents()).rejects.toThrow(
      'Research failed before completion',
    );
  });

  it('lève immédiatement une trame error du serveur', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => researchResponse([
      'data: {"type":"synthesis","text":"Fragment"}\n\n',
      'data: {"type":"error","message":"Synthèse interrompue"}\n\n',
      'data: {"type":"done","status":"error"}\n\n',
    ])));

    await expect(collectResearchEvents()).rejects.toThrow('Synthèse interrompue');
  });

  it('refuse une synthèse JSON malformée même si done annonce ensuite success', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => researchResponse([
      'data: {"type":"synthesis","text":}\n\n',
      'data: {"type":"done","status":"success"}\n\n',
    ])));

    await expect(collectResearchEvents()).rejects.toThrow(
      'Research stream returned malformed data',
    );
  });

  it('accepte une synthèse uniquement après un done success explicite', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => researchResponse([
      'data: {"type":"synth',
      'esis","text":"Réponse complète"}\n\n',
      'data: {"type":"done","status":"success","usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\n',
    ])));

    await expect(collectResearchEvents()).resolves.toEqual([
      { type: 'synthesis', text: 'Réponse complète' },
      {
        type: 'done',
        status: 'success',
        usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 },
      },
    ]);
  });
});
