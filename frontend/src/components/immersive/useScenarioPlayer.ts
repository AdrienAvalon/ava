import { useEffect, useRef } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { demoScenarios, type Scenario } from './scenarios';

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Auto-play the demo scenarios in a loop.
 * Returns a { running, toggle } API to pause/resume.
 */
export function useScenarioPlayer(autoStart = true) {
  const store = useImmersiveStore;
  const stopFlag = useRef(false);
  const runningRef = useRef(false);

  async function typeInto(text: string, speed: number) {
    for (let i = 0; i <= text.length; i++) {
      if (stopFlag.current) return;
      store.getState().setAvaMsg(text.slice(0, i));
      await sleep(speed);
    }
  }

  async function playOnce(s: Scenario) {
    const st = store.getState();

    // Listening
    st.setState('listening');
    st.setUserMsg(s.user);
    st.clearCognitive();
    st.setAvaMsg('');
    await sleep(s.listenMs);
    if (stopFlag.current) return;

    // Thinking
    st.setState('thinking');
    st.setCognitive(s.cognitive);
    await sleep(s.thinkMs);
    if (stopFlag.current) return;

    // Speaking (typewriter)
    st.setState('speaking');
    st.setUserMsg('');
    await typeInto(s.ava, s.speakSpeed);
    if (stopFlag.current) return;

    // Settle idle
    await sleep(s.settleMs ?? 2800);
    st.setState('idle');
    st.clearCognitive();
    await sleep(2200);
    st.setAvaMsg('');
  }

  async function run() {
    if (runningRef.current) return;
    runningRef.current = true;
    stopFlag.current = false;
    let i = 0;
    while (!stopFlag.current) {
      await playOnce(demoScenarios[i % demoScenarios.length]);
      i++;
    }
    runningRef.current = false;
  }

  function stop() {
    stopFlag.current = true;
  }

  useEffect(() => {
    if (autoStart) {
      const t = setTimeout(() => { run(); }, 600);
      return () => { clearTimeout(t); stop(); };
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStart]);

  return { run, stop, isRunning: () => runningRef.current };
}
