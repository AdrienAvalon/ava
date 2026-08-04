"""Tests du backend STT d'Ava — celui qui avait DISPARU sans que personne ne le voie.

⚠ POURQUOI CE FICHIER EXISTE. Le 2026-04-27, un commit intitulé « fix(ava security):
  allow microphone for self in Permissions-Policy » a supprimé **120 des 150 lignes** de
  `openai_whisper_ava_stt.py`, ne laissant que la docstring et les imports. Son corps de
  message annonçait un nettoyage d'instrumentation de debug — instrumentation qui
  n'existait PAS dans la version committée (vérifié : aucune trace de `ava-stt-dumps` ni
  de log de texte brut). La classe entière est partie avec.

  Rien ne l'a signalé pendant trois mois : le fichier s'importait toujours (les imports
  étaient intacts), `boot.py` continuait de l'importer sans erreur, et le daemon démarrait.
  Seule la dictée côté serveur avait disparu, silencieusement, au profit du navigateur.

  **Un test aurait crié le jour même.** C'est exactement ce que fait le premier de cette
  série : il vérifie que la classe est ENREGISTRÉE dans le registre, pas seulement que le
  module s'importe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openjarvis.core.registry import SpeechRegistry


def test_le_backend_est_enregistre() -> None:
    """⚠ LE TEST QUI AURAIT ATTRAPÉ LA SUPPRESSION DE 2026-04-27.

    Il ne se contente PAS d'importer le module : un fichier ampute de sa classe
    s'importe parfaitement (c'est ce qui s'est passe). Ce qui fait foi, c'est la
    presence de la cle dans le registre — donc l'execution reelle du decorateur.
    """
    import ava_extensions.backends.openai_whisper_ava_stt  # noqa: F401

    assert SpeechRegistry.contains("openai_ava"), (
        "backend 'openai_ava' absent du SpeechRegistry — la classe a-t-elle disparu ?"
    )
    classe = SpeechRegistry.get("openai_ava")
    assert classe is not None
    assert classe.__name__ == "OpenAIWhisperAvaBackend"


def test_boot_charge_le_backend() -> None:
    """⚠ Etre enregistrable ne suffit pas : encore faut-il que quelqu'un l'importe.

    Un backend jamais importe est du code mort que le registre ne signale pas — il
    reste simplement absent. `boot.py` est le seul point qui declenche les decorateurs.
    """
    from pathlib import Path

    boot = Path(__file__).resolve().parents[1] / "boot.py"
    assert "openai_whisper_ava_stt" in boot.read_text(), (
        "boot.py n'importe plus le backend STT : les decorateurs ne s'executeront jamais"
    )


# ── Le garde anti-hallucination — la raison d'etre de ce backend ────────────────────


@pytest.mark.parametrize(
    "texte",
    [
        "Sous-titres réalisés par la communauté d'Amara.org",
        "sous-titrage st' 501",
        "Merci d'avoir regardé cette vidéo !",
        "Abonnez-vous à la chaîne",
        "[Musique]",
        "Musique entraînante",
    ],
)
def test_hallucinations_connues_sont_filtrees(texte: str) -> None:
    """Whisper invente ces phrases sur du SILENCE ou du bruit de fond.

    ⚠ Sans ce filtre, Ava recevrait « Merci d'avoir regardé cette vidéo » comme une
      demande de l'utilisateur et y REPONDRAIT — le pire comportement possible pour un
      assistant vocal a l'ecoute permanente.
    """
    from ava_extensions.backends.openai_whisper_ava_stt import _looks_like_hallucination

    assert _looks_like_hallucination(texte) is True


@pytest.mark.parametrize(
    "texte",
    [
        "Quelle température fait-il dans le salon ?",
        "Allume la lumière du bureau",
        "Merci",
        "C'est une belle musique",
    ],
)
def test_la_parole_legitime_passe(texte: str) -> None:
    """⚠ LE CONTRE-TEST, ET IL COMPTE AUTANT QUE LE PRECEDENT.

    Un filtre qui bloque tout serait pire que pas de filtre : Ava deviendrait sourde
    sans qu'aucune erreur ne soit levee. Noter « Merci » et « C'est une belle musique » —
    ils contiennent des fragments proches des motifs filtres, et doivent passer.
    """
    from ava_extensions.backends.openai_whisper_ava_stt import _looks_like_hallucination

    assert _looks_like_hallucination(texte) is False


def test_le_filtre_ignore_les_accents_et_la_casse() -> None:
    """Whisper ne rend pas toujours les accents de la meme façon.

    Comparer des chaines accentuees telles quelles laisserait passer la moitie des
    hallucinations — d'ou la normalisation NFD du code.
    """
    from ava_extensions.backends.openai_whisper_ava_stt import _looks_like_hallucination

    assert _looks_like_hallucination(
        "SOUS-TITRES REALISES PAR LA COMMUNAUTE D'AMARA.ORG"
    )
    assert _looks_like_hallucination(
        "sous-titres réalisés   par la communauté  d'amara.org"
    )


def test_texte_vide_n_est_pas_une_hallucination() -> None:
    """Une transcription vide est un SILENCE, pas une invention — et le distinguer
    importe : le frontend ignore deja les transcriptions vides."""
    from ava_extensions.backends.openai_whisper_ava_stt import _looks_like_hallucination

    assert _looks_like_hallucination("") is False


# ── Contrat du backend ─────────────────────────────────────────────────────────────


def test_health_est_faux_sans_cle() -> None:
    """⚠ `health()` doit repondre FAUX sans cle, pas lever.

    Un backend qui plante a l'interrogation de sante fait tomber la decouverte des
    backends entiere — on perdrait aussi ceux qui fonctionnent.
    """
    from ava_extensions.backends.openai_whisper_ava_stt import OpenAIWhisperAvaBackend

    assert OpenAIWhisperAvaBackend(api_key="").health() is False


def test_formats_supportes_incluent_webm() -> None:
    """⚠ `webm` EST LOAD-BEARING : c'est le format que produit `MediaRecorder` dans le
    navigateur, donc le seul que la dictee d'Ava envoie reellement."""
    from ava_extensions.backends.openai_whisper_ava_stt import OpenAIWhisperAvaBackend

    assert "webm" in OpenAIWhisperAvaBackend(api_key="x").supported_formats()


def test_transcribe_sans_client_leve_une_erreur_explicite() -> None:
    """Sans cle, l'echec doit NOMMER sa cause. « missing API key » se diagnostique ;
    un AttributeError sur None envoie chercher un bug dans le code."""
    from ava_extensions.backends.openai_whisper_ava_stt import OpenAIWhisperAvaBackend

    with pytest.raises(RuntimeError, match="API key"):
        OpenAIWhisperAvaBackend(api_key="").transcribe(b"\x00", format="webm")


class _ReponseFactice:
    def __init__(self, texte: str) -> None:
        self.text = texte
        self.language = "fr"
        self.duration = 1.5


class _ClientFactice:
    """Imite `client.audio.transcriptions.create` — aucun appel reseau, aucun cout."""

    def __init__(self, texte: str) -> None:
        self._texte = texte
        self.dernier_appel: dict[str, Any] = {}

        parent = self

        class _Transcriptions:
            def create(self, **kwargs: Any) -> _ReponseFactice:
                parent.dernier_appel = kwargs
                return _ReponseFactice(parent._texte)

        class _Audio:
            transcriptions = _Transcriptions()

        self.audio = _Audio()


def _backend_avec(texte: str) -> Any:
    from ava_extensions.backends.openai_whisper_ava_stt import OpenAIWhisperAvaBackend

    b = OpenAIWhisperAvaBackend(api_key="factice")
    b._client = _ClientFactice(texte)
    return b


def test_transcribe_rend_le_texte_utile() -> None:
    b = _backend_avec("Quelle heure est-il ?")
    assert b.transcribe(b"\x00", format="webm").text == "Quelle heure est-il ?"


def test_transcribe_vide_une_hallucination() -> None:
    """⚠ LE COMPORTEMENT CENTRAL : on rend une chaine VIDE, on ne leve pas.

    Le frontend ignore les transcriptions vides ; lever ferait apparaitre une erreur a
    l'utilisateur pour un evenement parfaitement normal (du bruit capte par le micro).
    """
    b = _backend_avec("Sous-titres réalisés par la communauté d'Amara.org")
    assert b.transcribe(b"\x00", format="webm").text == ""


def test_le_decodage_est_deterministe() -> None:
    """`temperature=0` est ce qui empeche la derive creative sur audio faible.
    Le perdre reintroduirait les hallucinations que le filtre tente de rattraper."""
    b = _backend_avec("bonjour")
    b.transcribe(b"\x00", format="webm")
    assert b._client.dernier_appel["temperature"] == 0


def test_le_format_de_reponse_depend_du_modele() -> None:
    """⚠ Piege d'API : les modeles `gpt-4o-*transcribe` n'acceptent QUE `json` ou `text`,
    la `verbose_json` de whisper-1 leur vaut une erreur 400. Le backend doit choisir."""
    from ava_extensions.backends.openai_whisper_ava_stt import OpenAIWhisperAvaBackend

    b = OpenAIWhisperAvaBackend(api_key="x", model="gpt-4o-mini-transcribe")
    b._client = _ClientFactice("ok")
    b.transcribe(b"\x00", format="webm")
    assert b._client.dernier_appel["response_format"] == "json"

    b2 = OpenAIWhisperAvaBackend(api_key="x", model="whisper-1")
    b2._client = _ClientFactice("ok")
    b2.transcribe(b"\x00", format="webm")
    assert b2._client.dernier_appel["response_format"] == "verbose_json"


# ══ Le modele annonce vs le modele appele — corrige le 2026-08-04 ═════════════════


def test_le_modele_par_defaut_est_celui_que_la_docstring_ANNONCE() -> None:
    """⚠ La docstring promettait `gpt-4o-mini-transcribe` « instead of the legacy
    whisper-1 », et le constructeur posait `whisper-1`. Aucun appelant ne surchargeant
    ce parametre, le modele annonce n'a JAMAIS ete utilise.

    Une docstring fausse coute plus qu'un silence : on renonce a enqueter sur des
    hallucinations en croyant deja employer le modele qui les reduit. Ce test verrouille
    l'accord entre les deux — c'est le seul moyen d'empecher l'ecart de revenir.
    """
    import ava_extensions.backends.openai_whisper_ava_stt as m

    source = Path(m.__file__).read_text(encoding="utf-8")
    defaut = m.OpenAIWhisperAvaBackend()._model
    entete = source.split('"""')[1]
    # Le modele reellement pose doit apparaitre dans l'en-tete, et aucun autre modele
    # ne doit y etre presente comme celui qui est employe.
    assert defaut in entete, f"le defaut {defaut!r} n'est pas annonce dans la docstring"


def test_le_modele_est_configurable_par_environnement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le passage a `gpt-4o-mini-transcribe` doit etre possible sans modifier le code —
    mais rester un geste DELIBERE, pas un defaut silencieux."""
    monkeypatch.setenv("AVA_STT_MODEL", "gpt-4o-mini-transcribe")
    import ava_extensions.backends.openai_whisper_ava_stt as m

    assert m.OpenAIWhisperAvaBackend()._model == "gpt-4o-mini-transcribe"


@pytest.mark.parametrize(
    ("modele", "format_attendu"),
    [
        ("whisper-1", "verbose_json"),
        ("gpt-4o-mini-transcribe", "json"),
        ("gpt-4o-transcribe", "json"),
    ],
)
def test_le_format_de_reponse_suit_le_modele(modele: str, format_attendu: str) -> None:
    """⚠ CETTE BRANCHE ETAIT MORTE. `response_format` vaut `json` pour les modeles
    `gpt-4o-*` et `verbose_json` pour whisper — mais comme le modele etait toujours
    `whisper-1`, la moitie `json` n'a jamais ete executee. Maintenant que le modele est
    configurable, quelqu'un le changera : autant que le chemin soit eprouve avant.
    """
    import ava_extensions.backends.openai_whisper_ava_stt as m

    b = m.OpenAIWhisperAvaBackend(model=modele)
    assert (
        "json" if b._model.startswith("gpt-4o") else "verbose_json"
    ) == format_attendu
