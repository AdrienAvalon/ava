import { useEffect, useRef, useState } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { useViewportScale } from './useViewportScale';

/**
 * Terminal de conversation — l'historique complet de la session, défilant.
 *
 * ⚠ POURQUOI CE COMPOSANT EXISTE (2026-08-03, demande de l'admin : « il faudrait
 *   comme un terminal qui affiche la conversation et où on voit tout l'historique »).
 *   `ConversationCinetic` n'affiche QUE le dernier échange, en 34px au centre de
 *   l'écran : dès qu'Ava répond, la question disparaît ; dès qu'on repose une
 *   question, la réponse précédente est perdue. Rien n'était conservé, ni à l'écran
 *   ni en mémoire — on ne pouvait ni relire, ni vérifier ce qui avait été dit.
 *
 * ⚠ LES DEUX AFFICHAGES COEXISTENT, ET C'EST VOULU. La spec v2 fait du dernier
 *   échange le point focal (« présence », 34px, aura) ; ce terminal répond à un
 *   besoin différent — consulter. Remplacer l'un par l'autre perdrait à chaque fois
 *   quelque chose : sans le focal on perd la présence, sans le terminal on perd la
 *   mémoire. Le terminal est donc REPLIABLE, fermé par défaut sur petit écran.
 *
 * ⚠ Palette et typographie reprises de la spec v2, sans invention : monospace,
 *   cyan glacier pour Ava, bleu sourd pour l'utilisateur, fond noir absolu. Un
 *   second vocabulaire visuel dans la même page se lirait comme un autre logiciel.
 */
export function TranscriptTerminal() {
  const transcript = useImmersiveStore((s) => s.transcript);
  const clear = useImmersiveStore((s) => s.clearTranscript);
  const vp = useViewportScale();

  // Fermé par défaut sur mobile : l'écran y est déjà occupé par l'orbe et l'input.
  const [ouvert, setOuvert] = useState(() => !vp.isMobile);
  const [colleEnBas, setColleEnBas] = useState(true);
  const zone = useRef<HTMLDivElement>(null);

  // ⚠ DÉFILEMENT AUTOMATIQUE **CONDITIONNEL**. Suivre systématiquement le bas
  //   arracherait la vue à qui est en train de relire un échange plus haut — le
  //   défaut le plus courant des terminaux de chat, et le plus agaçant. On ne suit
  //   que si l'utilisateur était DÉJÀ en bas.
  useEffect(() => {
    if (!ouvert || !colleEnBas) return;
    const el = zone.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript, ouvert, colleEnBas]);

  function onScroll() {
    const el = zone.current;
    if (!el) return;
    // Marge de 24px : un défilement au pixel près ne se reproduit jamais exactement,
    // et exiger l'égalité stricte ferait décrocher le suivi sans raison visible.
    setColleEnBas(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
  }

  const largeur = vp.isMobile ? '92vw' : 'min(560px, 34vw)';

  if (!ouvert) {
    return (
      <button
        onClick={() => setOuvert(true)}
        title="Afficher l'historique de la conversation"
        style={{
          position: 'fixed', right: 16, bottom: vp.isMobile ? 88 : 24, zIndex: 220,
          background: 'rgba(81,164,222,0.08)', border: '1px solid rgba(81,164,222,0.3)',
          color: '#51a4de', fontFamily: 'inherit', fontSize: 10,
          letterSpacing: '0.25em', padding: '6px 12px', cursor: 'pointer',
          textTransform: 'uppercase',
        }}
      >
        ⌗ HISTORIQUE{transcript.length ? ` (${transcript.length})` : ''}
      </button>
    );
  }

  return (
    <div
      style={{
        position: 'fixed',
        right: vp.isMobile ? '4vw' : 24,
        bottom: vp.isMobile ? 80 : 24,
        width: largeur,
        maxHeight: vp.isMobile ? '46vh' : '62vh',
        zIndex: 220,
        display: 'flex',
        flexDirection: 'column',
        background: 'rgba(0,0,0,0.72)',
        border: '1px solid rgba(81,164,222,0.22)',
        backdropFilter: 'blur(2px)',
        fontFamily: "'JetBrains Mono', 'Space Mono', 'Courier New', monospace",
      }}
    >
      {/* En-tête */}
      <div
        style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '6px 10px', borderBottom: '1px solid rgba(81,164,222,0.18)',
          fontSize: 9, letterSpacing: '0.3em', color: '#5a7a95',
          textTransform: 'uppercase', flexShrink: 0,
        }}
      >
        <span>⌗ TRANSCRIPTION&nbsp;&nbsp;<span style={{ color: '#2a5a7a' }}>
          {transcript.length} ligne{transcript.length > 1 ? 's' : ''}
        </span></span>
        <span style={{ display: 'flex', gap: 10 }}>
          {transcript.length > 0 && (
            <button
              onClick={clear}
              title="Effacer l'historique affiché (la session serveur n'est pas touchée)"
              style={btnDiscret}
            >
              vider
            </button>
          )}
          <button onClick={() => setOuvert(false)} title="Replier" style={btnDiscret}>
            ✕
          </button>
        </span>
      </div>

      {/* Corps défilant */}
      <div
        ref={zone}
        onScroll={onScroll}
        style={{
          overflowY: 'auto', padding: '10px 12px', display: 'flex',
          flexDirection: 'column', gap: 10, fontSize: vp.isMobile ? 11 : 12,
          lineHeight: 1.55,
        }}
      >
        {transcript.length === 0 && (
          <div style={{ color: '#2a5a7a', fontSize: 10, letterSpacing: '0.2em' }}>
            — aucun échange dans cette session —
          </div>
        )}
        {transcript.map((l) => (
          <div key={l.id}>
            <div
              style={{
                fontSize: 9, letterSpacing: '0.25em', textTransform: 'uppercase',
                color: l.role === 'ava' ? '#3a8fc8' : l.role === 'user' ? '#2a5a7a' : '#7a5a5a',
                marginBottom: 2,
              }}
            >
              {l.role === 'ava' ? '⌈ AVA ⌉' : l.role === 'user' ? '⌈ ADRIEN ⌉' : '⌈ SYSTÈME ⌉'}
              <span style={{ opacity: 0.55, marginLeft: 8, letterSpacing: '0.1em' }}>
                {l.at}
              </span>
            </div>
            <div
              style={{
                // ⚠ `pre-wrap` : les réponses d'Ava contiennent des sauts de ligne et
                //   des listes. En `normal`, tout serait aplati en un paragraphe unique
                //   et le terminal deviendrait moins lisible que la vue centrale.
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                color: l.role === 'ava' ? '#b8cfe0' : l.role === 'user' ? '#7e9ab0' : '#c08a8a',
              }}
            >
              {l.text}
              {l.streaming && (
                <span style={{
                  display: 'inline-block', width: 2, height: '0.95em',
                  background: 'currentColor', marginLeft: 3, verticalAlign: 'middle',
                }} />
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Le suivi automatique est repris d'un geste, sans avoir à faire défiler. */}
      {!colleEnBas && (
        <button
          onClick={() => { setColleEnBas(true); const el = zone.current; if (el) el.scrollTop = el.scrollHeight; }}
          style={{
            ...btnDiscret, borderTop: '1px solid rgba(81,164,222,0.18)',
            padding: '5px 0', width: '100%', flexShrink: 0, letterSpacing: '0.25em',
          }}
        >
          ↓ revenir en bas
        </button>
      )}
    </div>
  );
}

const btnDiscret: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  color: '#5a7a95',
  fontFamily: 'inherit',
  fontSize: 9,
  letterSpacing: '0.2em',
  textTransform: 'uppercase',
  cursor: 'pointer',
  padding: 0,
};

export default TranscriptTerminal;
