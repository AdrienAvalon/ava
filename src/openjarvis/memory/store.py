"""Persistent stores for automatically extracted long-term memory facts.

A *fact* is a short, durable statement worth remembering about the user
(e.g. ``"Prefers concise answers"``).  Facts are produced by the memory
service's background extractor and persisted here so they survive across
sessions.  The store is intentionally small and self-contained: it dedupes,
caps the total number of facts, and is safe to call from multiple threads.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
import unicodedata
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List

from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import FactStoreRegistry


def _default_fact_path() -> Path:
    """Return the env-aware default JSONL path for automatic memory facts."""
    return get_config_dir() / "memory_facts.jsonl"


logger = logging.getLogger(__name__)


#: Marqueurs de PERISSABILITE. Un fait qui en contient un decrit un ETAT a un instant,
#: pas une propriete durable — et cet etat se lit en direct par les outils.
#: ⚠ Liste DERIVEE DES FAITS REELLEMENT ECRITS le 2026-08-05, pas imaginee : chacun de ces
#:   motifs attrape au moins un fait perime observe dans `memory_facts.jsonl`.
#: ⚠ Volontairement CONSERVATRICE — elle ne vise que des formulations sans ambiguite. Un
#:   fait durable formule avec « actuellement » n'est, par definition, pas durable.
_PERISSABLE = re.compile(
    r"""
    \bactuellement\b | \ben\s+ce\s+moment\b | \baujourd'?hui\b
    | \bhier\b | \bce\s+(?:matin|soir)\b | \bcet\s+apres[-\s]?midi\b
    | \bil\s+y\s+a\s+\d+ | \bderni(?:er|ere)\s+\w+\s+(?:effectue|realise)
    | \ben\s+\d+\s*(?:h|heures?|jours?|j)\b
    | \b\d+\s*/\s*100\b
    | \b\d{1,2}:\d{2}\b
    | \bcrash-?loop\b | \bredemarrages?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Sujets grammaticaux que l'extracteur ajoute ou omet au hasard. Les retirer avant de
#: comparer fait converger « Parle francais » et « L'utilisateur parle francais ».
_SUJETS = re.compile(
    r"^(?:l'utilisateur|l utilisateur|utilisateur|the\s+user|user|il|elle|on)\s+",
    re.IGNORECASE,
)


def _empreinte(texte: str) -> str:
    """Forme normalisee servant AU SEUL dedoublonnage.

    ⚠ On ne modifie JAMAIS le texte stocke : un fait doit rester lisible tel qu'il a ete
      formule. Cette empreinte ne sert qu'a repondre « est-ce que je sais deja ca ? ».
    """
    t = unicodedata.normalize("NFKD", texte.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _SUJETS.sub("", t.strip())
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


#: Mots vides des deux langues — ils gonflent artificiellement les recouvrements.
_VIDES = frozenset(
    {
        "utilisateur",
        "user",
        "avec",
        "pour",
        "dans",
        "leur",
        "cette",
        "sont",
        "avoir",
        "elle",
        "the",
        "has",
        "and",
        "with",
        "that",
        "this",
        "their",
        "have",
        "les",
        "des",
        "une",
        "son",
        "ses",
        "est",
        "aux",
        "que",
        "qui",
        "plus",
        "tout",
    }
)

#: Marqueurs d'anglais. Grossier, et suffisant : on ne s'en sert QUE pour departager deux
#: faits redondants, jamais pour rejeter un fait.
_ANGLAIS = re.compile(
    r"\b(the|user|has|with|and|of|is|are|their|monitors|speaks|manages)\b", re.I
)


def _mots_significatifs(texte: str) -> frozenset[str]:
    t = unicodedata.normalize("NFKD", texte.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return frozenset(m for m in re.findall(r"[a-z]{4,}", t) if m not in _VIDES)


def _est_anglais(texte: str) -> bool:
    return len(_ANGLAIS.findall(texte)) >= 2


def _redondant(nouveau: str, ancien: str) -> str | None:
    """Lequel des deux ne dit rien de plus ? Rend le texte a retirer, ou None.

    ⚠ INCLUSION STRICTE, pas recouvrement. Un fait n'est retire que si TOUS ses mots
      significatifs figurent deja dans l'autre — donc s'il n'apporte rien.
    ⚠ CAS MIXTE : quand un fait francais est inclus dans un fait anglais, garder « le plus
      informatif » garderait l'ANGLAIS, contre la regle d'ecriture en francais posee le
      2026-08-05. Mesure : le cas existe (« L'utilisateur vit avec Annie… » ⊂ « User has
      family members: Adrien… »). A information equivalente, la langue tranche.
    """
    mn, ma = _mots_significatifs(nouveau), _mots_significatifs(ancien)
    if not mn or not ma:
        return None
    if mn == ma:
        # Egalite de contenu : la langue tranche, sinon on garde l'existant.
        if _est_anglais(ancien) and not _est_anglais(nouveau):
            return ancien
        return nouveau
    if mn < ma:  # le nouveau n'apporte rien
        return (
            ancien if (_est_anglais(ancien) and not _est_anglais(nouveau)) else nouveau
        )
    if ma < mn:  # l'ancien est devenu redondant
        return (
            nouveau if (_est_anglais(nouveau) and not _est_anglais(ancien)) else ancien
        )
    return None


def _sans_accents(texte: str) -> str:
    """Forme sans diacritiques, pour appliquer les motifs ECRITS SANS ACCENTS.

    ⚠ CE HELPER MANQUAIT, ET SON ABSENCE RENDAIT `_PERISSABLE` A MOITIE INERTE.
      Les motifs sont volontairement ecrits sans accents (`apres-midi`, `derniere`,
      `redemarrages`) et etaient appliques au texte BRUT, qui est du francais accentue :
      `apres[-\\s]?midi` ne peut pas rencontrer « apres-midi » quand le fait dit
      « apres-midi » avec un accent grave. Trois des treize motifs ne pouvaient donc
      jamais declencher.
    ⚠ LE MESURER SUR LES FAITS STOCKES EST UN PIEGE : le filtre REFUSE a l'ecriture, donc
      les faits stockes sont les SURVIVANTS — un taux de correspondance nul y est le
      resultat attendu, pas une preuve d'inefficacite. Ce qui prouve le defaut, c'est le
      survivant accentue : « Annie et Jean-Pierre etaient presents a la maison cet
      apres-midi » est en memoire alors qu'il aurait du etre refuse (mesure 2026-08-06).
    """
    return "".join(
        c
        for c in unicodedata.normalize("NFD", texte)
        if unicodedata.category(c) != "Mn"
    )


@dataclass(slots=True)
class Fact:
    """A single durable memory entry.

    ⚠ ``perime_le`` MARQUE, IL NE SUPPRIME PAS — decision de l'admin du 2026-08-06.
      Un fait qui a cesse d'etre vrai dit encore ce qui ETAIT vrai, donc ce qui a change.
      Le supprimer efface cette histoire ; le taire fait enoncer a Ava du perime au
      present. Il reste donc sur le disque, il est servi au lecteur, et il est rendu avec
      sa date et sa raison de peremption.
    """

    text: str
    source: str = ""
    created_at: float = 0.0
    perime_le: float = 0.0
    perime_par: str = ""

    @property
    def perime(self) -> bool:
        return self.perime_le > 0


class FactStore(ABC):
    """Abstract persistent store for extracted memory facts."""

    @abstractmethod
    def add(self, text: str, source: str = "") -> bool:
        """Store *text* as a fact. Returns True if a new fact was stored."""

    def add_many(self, texts: Iterable[str], source: str = "") -> int:
        """Store several facts, returning the count of newly stored ones."""
        added = 0
        for text in texts:
            if self.add(text, source=source):
                added += 1
        return added

    @abstractmethod
    def list(self) -> List[Fact]:
        """Return all stored facts, oldest first."""

    @abstractmethod
    def clear(self) -> int:
        """Remove all stored facts, returning the number removed."""

    @abstractmethod
    def count(self) -> int:
        """Return the number of stored facts."""


@FactStoreRegistry.register("local")
@contextmanager
def _verrou_exclusif(chemin: Path):
    """Serialise le cycle lire-modifier-ecrire entre TOUS les ecrivains du fichier.

    ⚠ PERTE DE MISE A JOUR MESUREE LE 2026-08-07, sur la SEULE capacite d'ecriture d'Ava.
      Deux `mark_stale` emis dans la meme seconde ont tous deux rendu True et tous deux
      journalise « fait perime » — un seul a survecu sur le disque. Le second magasin
      avait charge le fichier AVANT que le premier ne l'ecrive, puis a reecrit sa propre
      copie par-dessus. Ava a donc annonce « les deux sont perimes » de bonne foi, et
      c'etait faux : un succes rapporte sans effet, exactement la classe de defaut que ce
      projet traque.

    ⚠ LE VERROU EN MEMOIRE NE SUFFISAIT PAS, et c'est ce qui rend le defaut invisible a
      la relecture : `self._lock` existe et fonctionne, mais l'appelant construit un
      `LocalFactStore` NEUF a chaque appel — donc deux verrous distincts qui ne se voient
      pas. Et la CLI (`openjarvis memory revive`) ecrit depuis un AUTRE processus, ou
      aucun verrou memoire ne peut porter.

    ⚠ Verrou sur un fichier SIDECAR, jamais sur le fichier de donnees lui-meme :
      `_flush` procede par `os.replace`, donc l'inode change a chaque ecriture et un
      verrou pose dessus protegerait un fichier qui n'existe deja plus. Meme piege que le
      bind-mount Grafana colle a son ancien inode.

    ⚠ NE BLOQUE JAMAIS INDEFINIMENT ET NE LEVE JAMAIS : si le verrou est indisponible
      (systeme de fichiers sans flock, permission refusee), on continue SANS. Perdre une
      ecriture concurrente est un defaut rare ; refuser d'ecrire du tout en serait un
      permanent.
    """
    verrou = None
    try:
        verrou = open(str(chemin) + ".lock", "a+")  # noqa: SIM115 — ferme dans le finally
        fcntl.flock(verrou.fileno(), fcntl.LOCK_EX)
    except Exception:  # noqa: BLE001
        if verrou is not None:
            verrou.close()
        verrou = None
    try:
        yield
    finally:
        if verrou is not None:
            try:
                fcntl.flock(verrou.fileno(), fcntl.LOCK_UN)
            finally:
                verrou.close()


class LocalFactStore(FactStore):
    """Append-only JSONL fact store on the local filesystem.

    Facts are kept human-readable (one JSON object per line) so they can be
    inspected or edited by hand.  Writes are atomic (temp file + rename) and
    guarded by a lock, so concurrent ``add`` calls from the extraction worker
    and ``list``/``clear`` from the CLI never corrupt the file.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_facts: int = 1000,
    ) -> None:
        self._path = (
            Path(path).expanduser() if path is not None else _default_fact_path()
        )
        self._max_facts = max(0, int(max_facts))
        self._lock = threading.Lock()
        self._facts: List[Fact] = self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> List[Fact]:
        if not self._path.exists():
            return []
        facts: List[Fact] = []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # skip malformed lines rather than crashing
            fact_text = str(obj.get("text", "")).strip()
            if not fact_text:
                continue
            facts.append(
                Fact(
                    text=fact_text,
                    source=str(obj.get("source", "")),
                    created_at=float(obj.get("created_at", 0.0) or 0.0),
                    # ⚠ Absents des lignes ecrites avant le 2026-08-06 : le defaut vaut
                    #   « courant », donc un ancien fichier se relit sans migration.
                    perime_le=float(obj.get("perime_le", 0.0) or 0.0),
                    perime_par=str(obj.get("perime_par", "") or ""),
                )
            )
        return facts

    def _flush(self) -> None:
        """Atomically rewrite the JSONL file from the in-memory list."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = "".join(
            json.dumps(asdict(f), ensure_ascii=False) + "\n" for f in self._facts
        )
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)

    def _sync_from_disk_locked(self) -> None:
        """Refresh in-memory facts from disk while holding ``self._lock``."""
        self._facts = self._load()

    # -- FactStore API ------------------------------------------------------

    def add(self, text: str, source: str = "") -> bool:
        text = (text or "").strip()
        if not text:
            return False
        # ⚠ ON REFUSE L'ETAT MESURABLE — mais SEULEMENT quand l'extraction est automatique.
        #   Si l'utilisateur demande explicitement de retenir quelque chose, c'est son
        #   choix et il prime : `source="auto"` distingue les deux. Sans cette nuance, on
        #   casserait la memoire volontaire pour reparer la memoire subie.
        if source == "auto" and _PERISSABLE.search(_sans_accents(text)):
            logger.debug("fait perissable refuse: %s", text[:80])
            return False
        with _verrou_exclusif(self._path), self._lock:
            self._sync_from_disk_locked()
            # ⚠ COMPARAISON SUR L'EMPREINTE, pas sur la chaine exacte. Le dedoublonnage
            #   d'origine testait l'egalite stricte en minuscules : « Parle francais » et
            #   « L'utilisateur parle francais » passaient tous les deux. Mesure du
            #   2026-08-05 : ce seul fait etait present SEPT fois sur 105.
            empreinte = _empreinte(text)
            if any(_empreinte(f.text) == empreinte for f in self._facts):
                return False  # dedupe
            # ⚠ CURATION A L'ECRITURE — le dedoublonnage par empreinte ne voit que les
            #   reformulations de SUJET ; il laisse passer les PARAPHRASES. Mesure du
            #   2026-08-05 : 7 faits sur 119 etaient strictement inclus dans un autre.
            #   Deux sens, et le second compte autant : un fait nouveau qui n'apporte rien
            #   n'entre pas ; un fait ANCIEN devenu redondant SORT.
            a_retirer: list[str] = []
            for f in self._facts:
                perdant = _redondant(text, f.text)
                if perdant is None:
                    continue
                if perdant == text:
                    logger.debug("fait redondant refuse: %s", text[:80])
                    return False
                a_retirer.append(f.text)
            if a_retirer:
                logger.debug("faits devenus redondants retires: %d", len(a_retirer))
                self._facts = [f for f in self._facts if f.text not in a_retirer]
            self._facts.append(Fact(text=text, source=source, created_at=time.time()))
            self._evict_locked()
            self._flush()
        return True

    def _evict_locked(self) -> None:
        """Ramene le corpus sous le plafond. A appeler en tenant ``self._lock``.

        ⚠ L'EVICTION SUPPRIMAIT LES FAITS LES PLUS DURABLES, EN SILENCE. L'ancienne regle
          etait `self._facts[-max:]` : on garde les plus RECENTS, donc on jette les plus
          ANCIENS — c'est-a-dire ceux qui ont survecu le plus longtemps a la curation,
          donc les plus durables. « Le disjoncteur est derriere la porte verte » serait
          parti avant « Adrien a redemarre le conteneur ».
        ⚠ CE N'EST PAS THEORIQUE : mesure du 2026-08-06 — 168 faits accumules en 2,2 jours,
          soit ~75/jour pour un plafond de 1000. La premiere suppression tombe vers le
          17 aout. Il restait onze jours.
        ⚠ ORDRE RETENU : les faits PERIMES d'abord (ils ont deja perdu leur actualite, et
          leur role d'archive cede devant la place), du plus ancien au plus recent ; puis,
          seulement s'il le faut, les plus anciens courants. Cette seconde suppression est
          JOURNALISEE EN WARNING : perdre un fait durable doit laisser une trace, sinon la
          memoire se vide sans que rien ne le dise — la definition meme d'un angle mort.
        """
        if not self._max_facts or len(self._facts) <= self._max_facts:
            return
        a_retirer = len(self._facts) - self._max_facts
        perimes = [i for i, f in enumerate(self._facts) if f.perime]
        sacrifies = set(perimes[:a_retirer])
        if len(sacrifies) < a_retirer:
            manquants = a_retirer - len(sacrifies)
            courants = [i for i in range(len(self._facts)) if i not in sacrifies][
                :manquants
            ]
            sacrifies.update(courants)
            logger.warning(
                "memoire pleine (%d/%d) : %d fait(s) COURANT(s) supprime(s), le plus "
                "ancien etant %r",
                len(self._facts),
                self._max_facts,
                manquants,
                self._facts[courants[0]].text[:80] if courants else "",
            )
        self._facts = [f for i, f in enumerate(self._facts) if i not in sacrifies]

    def mark_stale(self, text: str, reason: str = "") -> bool:
        """Marque un fait comme n'etant plus d'actualite. NE LE SUPPRIME PAS.

        Rend True si un fait a ete marque, False s'il est introuvable ou deja marque.

        ⚠ La correspondance se fait sur l'EMPREINTE, comme le dedoublonnage : demander de
          perimer « parle francais » doit atteindre « L'utilisateur parle francais ».
          Exiger la chaine exacte rendrait la fonction inutilisable a la main.
        """
        cible = _empreinte(text or "")
        if not cible:
            return False
        # ⚠ Le verrou FICHIER encadre le cycle complet lire-modifier-ecrire : sans lui,
        #   deux marquages simultanes se perdent l'un l'autre (mesure du 2026-08-07).
        with _verrou_exclusif(self._path), self._lock:
            self._sync_from_disk_locked()
            for fact in self._facts:
                if _empreinte(fact.text) == cible and not fact.perime:
                    fact.perime_le = time.time()
                    fact.perime_par = (reason or "").strip()[:200]
                    self._flush()
                    logger.info("fait marque perime: %s", fact.text[:80])
                    return True
        return False

    def list(self) -> List[Fact]:
        with self._lock:
            self._sync_from_disk_locked()
            return list(self._facts)

    def clear(self) -> int:
        with self._lock:
            self._sync_from_disk_locked()
            removed = len(self._facts)
            self._facts = []
            if self._path.exists():
                try:
                    self._path.unlink()
                except OSError:
                    self._flush()
        return removed

    def count(self) -> int:
        with self._lock:
            self._sync_from_disk_locked()
            return len(self._facts)

    @property
    def path(self) -> Path:
        """Filesystem location of the JSONL store."""
        return self._path


def _ensure_fact_store_backends_registered() -> None:
    """Restore built-in fact-store registrations if a test cleared registries."""
    if not FactStoreRegistry.contains("local"):
        FactStoreRegistry.register_value("local", LocalFactStore)


def create_fact_store(
    backend: str = "local",
    *,
    path: str | Path | None = None,
    max_facts: int = 1000,
) -> FactStore:
    """Construct a fact store for the configured *backend*.

    Only the ``"local"`` (on-disk JSONL) backend is supported today; the
    registry-backed constructor exists so additional backends can be added
    without changing the service or CLI wiring.
    """
    _ensure_fact_store_backends_registered()
    key = (backend or "local").strip().lower()
    if not FactStoreRegistry.contains(key):
        supported = ", ".join(FactStoreRegistry.keys())
        raise ValueError(
            f"Unknown memory backend '{backend}'. Supported backends: {supported}"
        )
    return FactStoreRegistry.create(key, path, max_facts=max_facts)


__all__ = ["Fact", "FactStore", "LocalFactStore", "create_fact_store"]
