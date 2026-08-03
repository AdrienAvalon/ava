import { useEffect, useState } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { useViewportScale } from './useViewportScale';

const mono = "'JetBrains Mono', 'Space Mono', 'Courier New', monospace";

export function HudLayers() {
  const state = useImmersiveStore((s) => s.state);
  const [clock, setClock] = useState('');
  const vp = useViewportScale();

  useEffect(() => {
    const tick = () => setClock(new Date().toISOString().substring(11, 19));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const statusText = state === 'idle' ? 'ONLINE' : state.toUpperCase();
  const runtime = useImmersiveStore((s) => s.runtime);
  const brandSize = vp.smallHud ? 16 : 22;
  const sub = vp.smallHud ? 8 : 10;
  const statusSize = vp.smallHud ? 13 : 18;
  const edge = vp.isMobile ? 12 : 32;

  return (
    <>
      {/* ⚠ EN MODE CONVERSATION, IDENTITÉ ET STATUT SONT SUR LA MÊME LIGNE, À GAUCHE.
          C'est la correction STRUCTURELLE d'un défaut qu'on a tenté deux fois de régler
          en déplaçant des pixels : « ONLINE » était ancré en haut à DROITE, exactement là
          où se trouvent les boutons CHAT et VOIX, et les deux se chevauchaient jusqu'à
          devenir illisibles. Déplacer les boutons vers le bas ne faisait que décaler la
          collision d'un cran (constaté sur photo).
          Ici la droite est RÉSERVÉE aux boutons, la gauche à l'identité — plus aucune
          géométrie ne peut les faire se rencontrer. « COGNITIVE INTERFACE » et l'horloge
          sont retirés du téléphone : sur 390 px de large, ce sont des décorations qui
          coûtent la place dont les commandes ont besoin. */}
      {vp.chatFirst ? (
        <div style={{
          position: 'fixed', top: 12, left: edge, zIndex: 100, pointerEvents: 'none',
          fontFamily: mono, display: 'flex', alignItems: 'baseline', gap: 10,
        }}>
          <span style={{
            fontSize: 15, fontWeight: 700, letterSpacing: '0.3em',
            color: '#51a4de', textShadow: '0 0 16px rgba(81,164,222,0.6)',
          }}>AVA</span>
          <span style={{
            fontSize: 9, fontWeight: 700, letterSpacing: '0.25em', color: '#3a8fc8',
          }}>{statusText}</span>
        </div>
      ) : (
        <>
          {/* Top-left: identity */}
          <div style={{ position: 'fixed', top: 28, left: edge, zIndex: 100, pointerEvents: 'none', fontFamily: mono }}>
            <div style={{
              fontSize: brandSize, fontWeight: 700, letterSpacing: '0.3em',
              color: '#51a4de', textShadow: '0 0 16px rgba(81,164,222,0.6)',
            }}>AVA</div>
            <div style={{ fontSize: sub, letterSpacing: '0.35em', color: '#5a7a95', marginTop: 4 }}>COGNITIVE INTERFACE</div>
            {!vp.isMobile && (
              <div style={{ fontSize: 9, letterSpacing: '0.3em', color: '#2a5a7a', marginTop: 12, opacity: 0.7 }}>
                v2.0 // NEURAL-SYNC 0x7A3F
              </div>
            )}
          </div>

          {/* Top-right: status + session + clock */}
          <div style={{ position: 'fixed', top: 28, right: edge, zIndex: 100, pointerEvents: 'none', textAlign: 'right', fontFamily: mono }}>
            <div style={{
              fontSize: statusSize, fontWeight: 700, letterSpacing: '0.25em',
              color: '#51a4de', textShadow: '0 0 12px rgba(81,164,222,0.5)',
            }}>{statusText}</div>
            {!vp.isMobile && (
              <>
                <div style={{ fontSize: 10, letterSpacing: '0.2em', color: '#5a7a95', marginTop: 4 }}>SESSION 2E57F8D3</div>
                <div style={{ fontSize: 10, letterSpacing: '0.2em', color: '#5a7a95', marginTop: 2 }}>{clock} UTC</div>
              </>
            )}
          </div>
        </>
      )}

      {/* Bottom-left: stack tech (hidden on mobile, narrow) */}
      {!vp.isMobile && (
        <div style={{
          position: 'fixed', bottom: 28, left: edge, zIndex: 100, pointerEvents: 'none', fontFamily: mono,
          display: 'grid', gridTemplateColumns: 'auto auto', gap: '2px 18px',
          fontSize: 10, letterSpacing: '0.18em',
        }}>
          {/* ⚠ CES VALEURS ÉTAIENT ÉCRITES EN DUR — ET DEUX SUR QUATRE ÉTAIENT FAUSSES.
              Le HUD affichait « kokoro-ff_siwis » alors que le client demande OpenAI nova
              (`useDaemonChat.ts`), et « whisper-large-v3 » alors que le backend STT
              serveur est hors service. Un tableau de bord qui affirme une configuration
              qu'il ne lit pas est pire qu'un tableau vide : on croit savoir.
              Les valeurs viennent maintenant du store, alimenté par les réponses réelles
              du daemon (en-tête `X-Ava-TTS-Backend`), et affichent « — » tant qu'aucun
              échange n'a eu lieu — ce qui est honnête : avant le premier appel, on ne
              SAIT pas ce que le serveur utilisera. */}
          <div style={{ color: '#5a7a95' }}>MODEL</div>   <div style={{ color: '#7fb9e8', textAlign: 'right' }}>{runtime.model ?? '—'}</div>
          <div style={{ color: '#5a7a95' }}>ENGINE</div>  <div style={{ color: '#7fb9e8', textAlign: 'right' }}>{runtime.engine ?? '—'}</div>
          <div style={{ color: '#5a7a95' }}>TTS</div>     <div style={{ color: '#7fb9e8', textAlign: 'right' }}>{runtime.tts ?? '—'}</div>
          <div style={{ color: '#5a7a95' }}>STT</div>     <div style={{ color: '#7fb9e8', textAlign: 'right' }}>{runtime.stt ?? '—'}</div>
        </div>
      )}

      {/* Bottom-right: boot log (hidden on mobile) */}
      {!vp.smallHud && (
        <div style={{
          position: 'fixed', bottom: 28, right: edge, zIndex: 100, pointerEvents: 'none',
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
      )}

      {/* Right-edge katakana deco (vertical, desktop only) */}
      {!vp.hideKatakana && (
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
      )}
    </>
  );
}
