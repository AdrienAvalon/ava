import { useCallback, useEffect, useRef } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { demoScenarios, type Scenario } from './scenarios';

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export function useScenarioPlayer(autoStart = true) {
  const stopFlag = useRef(false);
  const runningRef = useRef(false);

  const playOnce = useCallback(async (s: Scenario) => {
    const st = useImmersiveStore.getState();

    st.setState('listening');
    st.setUserMsg(s.user);
    st.clearCognitive();
    st.setAvaMsg('');
    await sleep(s.listenMs);
    if (stopFlag.current) return;

    st.setState('thinking');
    st.setCognitive(s.cognitive);
    await sleep(s.thinkMs);
    if (stopFlag.current) return;

    st.setState('speaking');
    st.setUserMsg('');
    for (let i = 0; i <= s.ava.length; i++) {
      if (stopFlag.current) return;
      st.setAvaMsg(s.ava.slice(0, i));
      await sleep(s.speakSpeed);
    }

    await sleep(s.settleMs ?? 2800);
    if (stopFlag.current) return;
    st.setState('idle');
    st.clearCognitive();
    await sleep(2200);
    st.setAvaMsg('');
  }, []);

  const run = useCallback(async () => {
    if (runningRef.current) return;
    runningRef.current = true;
    stopFlag.current = false;
    let i = 0;
    while (!stopFlag.current) {
      await playOnce(demoScenarios[i % demoScenarios.length]);
      i++;
    }
    runningRef.current = false;
  }, [playOnce]);

  const stop = useCallback(() => {
    stopFlag.current = true;
  }, []);

  const isRunning = useCallback(() => runningRef.current, []);

  useEffect(() => {
    if (autoStart) {
      const t = setTimeout(() => { run(); }, 600);
      return () => { clearTimeout(t); stop(); };
    }
    return undefined;
  }, [autoStart, run, stop]);

  return { run, stop, isRunning };
}
