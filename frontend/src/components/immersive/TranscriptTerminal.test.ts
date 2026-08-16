import { describe, expect, it, vi } from 'vitest';
import { effacerTranscriptApresConfirmation } from './TranscriptTerminal';

describe('effacerTranscriptApresConfirmation', () => {
  it('conserve le transcript local quand le serveur refuse la suppression', async () => {
    const clear = vi.fn();

    await expect(
      effacerTranscriptApresConfirmation(clear, async () => false),
    ).resolves.toBe(false);

    expect(clear).not.toHaveBeenCalled();
  });

  it('vide le transcript local seulement après acquittement serveur', async () => {
    const clear = vi.fn();

    await expect(
      effacerTranscriptApresConfirmation(clear, async () => true),
    ).resolves.toBe(true);

    expect(clear).toHaveBeenCalledOnce();
  });
});
