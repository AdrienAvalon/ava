import { create } from 'zustand';
import type { ImmersiveState } from './immersiveStates';

export interface CognitiveSignals {
  intent: string | null;
  focus: string | null;
  tool: string | null;
  reflection: string | null;
  tone: string | null;
  memory: string | null;
}

interface ImmersiveStore {
  state: ImmersiveState;
  userMsg: string;
  avaMsg: string;
  cognitive: CognitiveSignals;
  rippleKey: number; // bump to trigger ripple animation

  setState: (s: ImmersiveState) => void;
  setUserMsg: (m: string) => void;
  setAvaMsg: (m: string) => void;
  setCognitive: (c: Partial<CognitiveSignals>) => void;
  clearCognitive: () => void;
}

const emptyCognitive: CognitiveSignals = {
  intent: null,
  focus: null,
  tool: null,
  reflection: null,
  tone: null,
  memory: null,
};

export const useImmersiveStore = create<ImmersiveStore>((set) => ({
  state: 'idle',
  userMsg: '',
  avaMsg: '',
  cognitive: { ...emptyCognitive },
  rippleKey: 0,

  setState: (s) => set((prev) => ({ state: s, rippleKey: prev.rippleKey + 1 })),
  setUserMsg: (userMsg) => set({ userMsg }),
  setAvaMsg: (avaMsg) => set({ avaMsg }),
  setCognitive: (c) => set((prev) => ({ cognitive: { ...prev.cognitive, ...c } })),
  clearCognitive: () => set({ cognitive: { ...emptyCognitive } }),
}));
