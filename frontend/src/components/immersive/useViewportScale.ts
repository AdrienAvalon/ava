import { useEffect, useState } from 'react';

export interface ViewportScale {
  width: number;
  height: number;
  isMobile: boolean;
  isTablet: boolean;
  isNarrow: boolean;
  fontScale: number;    // 1.0 desktop → 0.7 mobile
  orbScale: number;     // overall orb group scale factor
  hideCognitive: boolean;
  hideKatakana: boolean;
  smallHud: boolean;

  /**
   * ⚠ MODE « CONVERSATION D'ABORD » — la hiérarchie du téléphone est INVERSÉE par
   *   rapport à celle du bureau, et c'est délibéré (retour de l'admin, 2026-08-03 :
   *   « on ne voit pas le terminal de chat, et on voit surtout qu'on peut utiliser le
   *   mode vocal alors qu'il est plutôt secondaire »).
   *
   *   Sur grand écran, l'orbe est le sujet : on regarde Ava, on lui parle, le dernier
   *   échange s'affiche en grand au centre. Sur un téléphone tenu à la main, ça ne
   *   marche pas — l'orbe mangeait 60 % de la hauteur, les deux gros boutons vocaux
   *   occupaient le centre, et la conversation se réduisait à un bouton replié dans un
   *   coin. L'interface annonçait donc une intention (« parle-moi ») qui n'est pas
   *   l'usage principal.
   *
   *   En mode `chatFirst` : la conversation occupe l'espace utile, la saisie texte est
   *   en bas avec un micro DISCRET à côté (comme une application de messagerie), et
   *   l'orbe redevient ce qu'elle doit être à cette taille — un décor.
   */
  chatFirst: boolean;
  /**
   * En mode conversation, y a-t-il la place de mettre l'orbe À CÔTÉ du terminal ?
   *
   * ⚠ Deux dispositions, parce qu'un portable et un téléphone n'ont pas le même problème.
   *   · Portable (≥ 900 px) : le terminal prend la gauche, l'orbe occupe la colonne de
   *     droite. Les deux sont pleinement visibles — c'est ce que l'admin demandait.
   *   · Téléphone : aucune largeur pour deux colonnes. Le terminal prend tout et l'orbe
   *     se réduit à une vignette en haut à droite, signe de présence discret.
   *   Forcer la première disposition sur un téléphone donnerait un terminal de 250 px
   *   de large, illisible ; forcer la seconde sur un portable gâcherait la moitié de
   *   l'écran.
   */
  orbeACote: boolean;
  /** Hauteur réservée au bandeau supérieur — les composants s'y accordent au lieu de
   *  se chevaucher, ce qui est précisément ce qui produisait la superposition
   *  « ONLINE » / « CHAT » corrigée deux fois sans succès à coups de pixels. */
  topBar: number;
  /** Hauteur réservée à la barre de saisie du bas (saisie + micro). */
  bottomBar: number;
}

function compute(w: number, h: number): ViewportScale {
  const isMobile = w < 640;
  const isTablet = w < 960;
  const isNarrow = w < 1200;
  // ⚠ `chatFirst` COUVRE DÉSORMAIS LE PORTABLE, PAS SEULEMENT LE TÉLÉPHONE (2026-08-04).
  //   Il valait `isMobile` (< 640 px) : sur un portable en Firefox, l'admin voyait donc
  //   toujours l'ancienne mise en page — orbe plein écran, conversation reléguée dans une
  //   petite carte — et a redemandé trois fois la même chose. Je croyais avoir répondu à
  //   sa demande ; je n'avais répondu que pour une largeur d'écran qu'il n'utilise pas.
  //   Le seuil est maintenant < 1400 px : la vue « orbe au centre » ne subsiste que sur
  //   un grand écran, là où l'orbe et la conversation tiennent VRAIMENT côte à côte.
  const chatFirst = w < 1400;
  return {
    width: w,
    height: h,
    isMobile,
    isTablet,
    isNarrow,
    // ⚠ L'orbe passe à 0,52 en mode conversation (contre 0,85) : elle reste présente —
    //   c'est l'identité visuelle du produit — mais cesse d'être le sujet de l'écran.
    fontScale: isMobile ? 0.58 : isTablet ? 0.75 : 1.0,
    // ⚠ 0,95 quand l'orbe a sa propre colonne (portable) : elle y dispose de la place,
    //   la rapetisser la rendrait insignifiante. 0,52 en vignette (téléphone), où elle
    //   n'est qu'un signe de présence par-dessus le terminal.
    orbScale: chatFirst ? (w >= 900 ? 0.95 : 0.52) : isTablet ? 1.0 : 1.3,
    hideCognitive: isMobile,       // cognitive panel hidden on phones
    hideKatakana: isTablet,        // right-edge katakana hidden on tablets and smaller
    smallHud: isTablet,
    chatFirst,
    orbeACote: chatFirst && w >= 900,
    topBar: chatFirst ? 46 : 0,
    bottomBar: chatFirst ? 64 : 0,
  };
}

export function useViewportScale(): ViewportScale {
  const [scale, setScale] = useState<ViewportScale>(() =>
    compute(
      typeof window !== 'undefined' ? window.innerWidth : 1600,
      typeof window !== 'undefined' ? window.innerHeight : 900,
    ),
  );
  useEffect(() => {
    let raf = 0;
    const onResize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        setScale(compute(window.innerWidth, window.innerHeight));
      });
    };
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      cancelAnimationFrame(raf);
    };
  }, []);
  return scale;
}
