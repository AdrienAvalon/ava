/**
 * Amplitude de la voix d'Ava, en temps réel — pour que son avatar parle vraiment.
 *
 * ⚠ POURQUOI CE MODULE (2026-08-04, demande de l'admin : « améliorer le dynamisme de
 *   l'avatar central quand elle parle, que ça fasse plus naturel et organique, comme si
 *   son avatar parlait, à sa manière »).
 *
 *   L'orbe interpolait vers des valeurs CONSTANTES par état : en `speaking`, elle
 *   prenait la cible « speaking » et s'y installait. Elle changeait donc d'apparence au
 *   début de la parole puis restait figée jusqu'à la fin — un état, pas un mouvement.
 *   Une animation périodique fabriquée (sinus, bruit) aurait donné du mouvement, mais
 *   pas de la PAROLE : le rythme n'aurait eu aucun rapport avec ce qui est prononcé, et
 *   l'œil le repère immédiatement — c'est ce qui distingue une bouche animée d'une bouche
 *   qui articule.
 *
 *   On branche donc l'orbe sur le signal audio RÉEL, via un `AnalyserNode` inséré dans
 *   la chaîne de lecture. Les silences entre les mots, les attaques de syllabes, la
 *   respiration des phrases : tout ça pilote directement la sphère. C'est gratuit
 *   (l'audio existe déjà) et exact par construction.
 *
 * ⚠ REPLI SYNTHÉTIQUE OBLIGATOIRE. Ava peut parler sans audio : voix coupée (bouton
 *   MUTE), TTS en échec, ou navigateur qui bloque la lecture faute de geste utilisateur.
 *   Sans repli, l'orbe resterait parfaitement immobile pendant qu'un texte défile — pire
 *   que l'animation constante d'avant, parce qu'elle donnerait l'air d'être plantée.
 *   Le repli n'imite pas la voix : il donne une respiration lente et irrégulière, qui se
 *   lit comme « elle est en train de produire quelque chose ».
 */

let analyseur: AnalyserNode | null = null;
// ⚠ `Uint8Array<ArrayBuffer>` explicite : TypeScript 7 distingue désormais le tampon
//   sous-jacent, et `getByteFrequencyData` refuse un `ArrayBufferLike` (qui pourrait
//   être un `SharedArrayBuffer`).
let tampon: Uint8Array<ArrayBuffer> | null = null;

/** Amplitude lissée [0..1], lue à chaque image par l'orbe. */
let niveau = 0;

/**
 * Insère un analyseur dans la chaîne de lecture.
 *
 * ⚠ L'analyseur est branché EN DÉRIVATION, pas en série : la source va à la fois vers
 *   la destination (le son sort) et vers l'analyseur (on le mesure). Le mettre en série
 *   fonctionnerait aussi, mais toute erreur future dans ce module couperait le son —
 *   un défaut d'affichage ne doit jamais rendre Ava muette.
 */
export function brancherAnalyseur(ctx: AudioContext, source: AudioNode): void {
  try {
    if (!analyseur) {
      analyseur = ctx.createAnalyser();
      // 256 → 128 valeurs. Assez fin pour suivre les syllabes (~10 Hz), assez grossier
      // pour ne pas coûter à chaque image.
      analyseur.fftSize = 256;
      // ⚠ Lissage à 0,55 : plus bas, l'orbe tremble sur chaque consonne ; plus haut,
      //   elle traîne derrière la voix et l'effet « ça parle » disparaît.
      analyseur.smoothingTimeConstant = 0.55;
      tampon = new Uint8Array(analyseur.frequencyBinCount);
    }
    source.connect(analyseur);
  } catch {
    /* l'analyse n'est qu'un confort visuel — jamais une raison d'échouer */
  }
}

/** Signale la fin de la lecture : l'orbe doit retomber. */
export function relacherAnalyseur(): void {
  niveau = 0;
}

/**
 * Amplitude courante [0..1].
 *
 * ⚠ Appelée à CHAQUE IMAGE (60 fois par seconde) : elle doit rester bon marché et ne
 *   jamais lever. D'où la lecture directe du tampon, sans allocation.
 */
export function amplitudeVoix(): number {
  if (!analyseur || !tampon) return 0;
  try {
    analyseur.getByteFrequencyData(tampon);
    // Moyenne sur le bas du spectre : la voix humaine y concentre son énergie, et les
    // aigus ne feraient qu'ajouter du bruit à la mesure.
    const bornes = Math.floor(tampon.length * 0.6);
    let somme = 0;
    for (let i = 0; i < bornes; i++) somme += tampon[i];
    const brut = somme / bornes / 255;
    // Lissage asymétrique : montée rapide (l'attaque d'une syllabe doit se voir),
    // descente plus lente (sinon l'orbe clignote entre deux mots).
    niveau = brut > niveau ? niveau + (brut - niveau) * 0.5 : niveau + (brut - niveau) * 0.12;
    return Math.min(1, niveau);
  } catch {
    return 0;
  }
}

/**
 * Repli : une respiration irrégulière, quand aucun audio n'est disponible.
 *
 * ⚠ Trois sinusoïdes de périodes non multiples entre elles. Une seule donnerait un
 *   battement mécanique, immédiatement identifiable comme une boucle ; leur somme
 *   présente une quasi-période de l'ordre de 8 secondes — mesurée, pas supposée. J'avais
 *   d'abord écrit ici « ne se répète qu'au bout de plusieurs minutes », ce qui était faux,
 *   et la mesure l'a montré.
 *   ⚠ Ce n'est PAS un défaut à corriger : ce repli ne s'affiche que lorsqu'aucun audio
 *   n'est disponible (voix coupée, TTS en échec), et pendant quelques secondes de
 *   réponse. Une quasi-période de 8 s n'a pas le temps de se voir. Chercher mieux
 *   reviendrait à optimiser un chemin dégradé au détriment du chemin normal, qui est
 *   piloté par la vraie voix.
 */
export function respirationSynthetique(t: number): number {
  const a = Math.sin(t * 2.3) * 0.5 + 0.5;
  const b = Math.sin(t * 3.7 + 1.1) * 0.5 + 0.5;
  const c = Math.sin(t * 1.3 + 2.6) * 0.5 + 0.5;
  return (a * 0.5 + b * 0.3 + c * 0.2) * 0.7;
}
