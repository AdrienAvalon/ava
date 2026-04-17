import { useEffect, useState } from 'react';
import { useImmersiveStore } from './immersiveStore';

const mono = "'JetBrains Mono', 'Space Mono', 'Courier New', monospace";

export function HudLayers() {
  const state = useImmersiveStore((s) => s.state);
  const [clock, setClock] = useState('');

  useEffect(() => {
    const tick = () => setClock(new Date().toISOString().substring(11, 19));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const statusText = state === 'idle' ? 'ONLINE' : state.toUpperCase();

  return (
    <>
      {/* Top-left: identity */}
      <div style={{ position: 'fixed', top: 28, left: 32, zIndex: 100, pointerEvents: 'none', fontFamily: mono }}>
        <div style={{
          fontSize: 22, fontWeight: 700, letterSpacing: '0.3em',
          color: '#51a4de', textShadow: '0 0 16px rgba(81,164,222,0.6)',
        }}>AVA</div>
        <div style={{ fontSize: 10, letterSpacing: '0.35em', color: '#5a7a95', marginTop: 4 }}>COGNITIVE INTERFACE</div>
        <div style={{ fontSize: 9, letterSpacing: '0.3em', color: '#2a5a7a', marginTop: 12, opacity: 0.7 }}>
          v2.0 // NEURAL-SYNC 0x7A3F
        </div>
      </div>

      {/* Top-right: status + session + clock */}
      <div style={{ position: 'fixed', top: 28, right: 32, zIndex: 100, pointerEvents: 'none', textAlign: 'right', fontFamily: mono }}>
        <div style={{
          fontSize: 18, fontWeight: 700, letterSpacing: '0.25em',
          color: '#51a4de', textShadow: '0 0 12px rgba(81,164,222,0.5)',
        }}>{statusText}</div>
        <div style={{ fontSize: 10, letterSpacing: '0.2em', color: '#5a7a95', marginTop: 4 }}>SESSION 2E57F8D3</div>
        <div style={{ fontSize: 10, letterSpacing: '0.2em', color: '#5a7a95', marginTop: 2 }}>{clock} UTC</div>
      </div>

      {/* Bottom-left: stack tech */}
      <div style={{
        position: 'fixed', bottom: 28, left: 32, zIndex: 100, pointerEvents: 'none', fontFamily: mono,
        display: 'grid', gridTemplateColumns: 'auto auto', gap: '2px 18px',
        fontSize: 10, letterSpacing: '0.18em',
      }}>
        <div style={{ color: '#5a7a95' }}>MODEL</div>   <div style={{ color: '#7fb9e8', textAlign: 'right' }}>claude-sonnet-4-6</div>
        <div style={{ color: '#5a7a95' }}>ENGINE</div>  <div style={{ color: '#7fb9e8', textAlign: 'right' }}>anthropic/cloud</div>
        <div style={{ color: '#5a7a95' }}>TTS</div>     <div style={{ color: '#7fb9e8', textAlign: 'right' }}>kokoro-ff_siwis</div>
        <div style={{ color: '#5a7a95' }}>STT</div>     <div style={{ color: '#7fb9e8', textAlign: 'right' }}>whisper-large-v3</div>
      </div>

      {/* Bottom-right: boot log + katakana */}
      <div style={{
        position: 'fixed', bottom: 28, right: 32, zIndex: 100, pointerEvents: 'none',
        textAlign: 'right', fontSize: 10, color: '#5a7a95', letterSpacing: '0.15em',
        lineHeight: 1.7, fontFamily: mono,
      }}>
        <div style={{ fontFamily: "'MS Gothic', monospace", fontSize: 9, color: '#2a5a7a', marginBottom: 6, opacity: 0.7, letterSpacing: '0.1em' }}>
          アヴァ・オンライン // 認知インターフェース
        </div>
        <div>&gt; boot.sequence.complete</div>
        <div>&gt; persona.ava_v1 loaded</div>
        <div>&gt; awaiting user input...</div>
      </div>

      {/* Right-edge katakana deco (vertical) */}
      <div style={{
        position: 'fixed', top: '50%', right: 40,
        transform: 'translateY(-50%)',
        writingMode: 'vertical-rl',
        fontFamily: "'MS Gothic', monospace",
        fontSize: 14, color: '#2a5a7a',
        letterSpacing: '0.5em', opacity: 0.25,
        zIndex: 50, pointerEvents: 'none',
      }}>
        サイバーネティック・ゴースト
      </div>
    </>
  );
}
