"""Tests de la sonde de routage.

⚠ CE QUI EST TESTE ICI N'EST PAS « la sonde ecrit bien » mais **« la sonde ne peut pas
  nuire »**. Une sonde est du code qui s'execute a chaque echange, dans le thread qui
  publie l'evenement (l'EventBus appelle ses abonnes de façon synchrone). Si elle leve,
  elle casse la conversation ; si elle ecrit mal, elle rend la mesure inexploitable —
  c'est-a-dire qu'elle detruit sa propre raison d'etre.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.telemetry import routing_probe


class _Evenement:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


@pytest.fixture(autouse=True)
def _journal_temporaire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    chemin = tmp_path / "routing.jsonl"
    monkeypatch.setattr(routing_probe, "CHEMIN", chemin)
    routing_probe._etats.clear()
    return chemin


def _lignes(chemin: Path) -> list[dict[str, Any]]:
    if not chemin.exists():
        return []
    return [json.loads(x) for x in chemin.read_text(encoding="utf-8").splitlines() if x]


# ── Ce que la sonde doit capturer ──────────────────────────────────────────────────


def test_un_echange_produit_une_ligne(_journal_temporaire: Path) -> None:
    routing_probe._sur_echange_termine(_Evenement({"user_message": "Bonjour"}))
    lignes = _lignes(_journal_temporaire)
    assert len(lignes) == 1
    assert lignes[0]["longueur_question"] == 7


def test_l_appel_d_outil_est_le_signal_decisif(_journal_temporaire: Path) -> None:
    """⚠ LE COEUR DE LA MESURE, et la raison d'etre de cette sonde.

    « Il fait quoi dehors ? » est COURT — score de complexite bas — et declenche
    pourtant un appel d'outil. C'est precisement ce cas que la sonde doit rendre
    visible : router sur la longueur enverrait cette question au modele le moins
    capable de la traiter.
    """
    routing_probe._sur_debut_outil(_Evenement({"tool": "home_assistant"}))
    routing_probe._sur_echange_termine(
        _Evenement({"user_message": "Il fait quoi dehors ?"})
    )

    ligne = _lignes(_journal_temporaire)[0]
    assert ligne["outil_appele"] is True
    assert ligne["outils"] == ["home_assistant"]
    # La question est courte : c'est exactement le piege qu'on veut documenter.
    assert ligne["longueur_question"] < 30


def test_les_jetons_de_PLUSIEURS_inferences_s_additionnent(
    _journal_temporaire: Path,
) -> None:
    """⚠ Un echange avec outil produit PLUSIEURS inferences : une pour decider de
    l'appel, une pour rediger la reponse. Garder la derniere sous-estimerait le cout
    reel — soit exactement la grandeur qu'on cherche a mesurer.
    """
    routing_probe._sur_fin_inference(
        _Evenement({"prompt_tokens": 100, "completion_tokens": 20})
    )
    routing_probe._sur_fin_inference(
        _Evenement({"prompt_tokens": 300, "completion_tokens": 80})
    )
    routing_probe._sur_echange_termine(_Evenement({"user_message": "x"}))

    ligne = _lignes(_journal_temporaire)[0]
    assert ligne["jetons_entree"] == 400
    assert ligne["jetons_sortie"] == 100


def test_le_modele_reellement_utilise_est_note(_journal_temporaire: Path) -> None:
    """On veut comparer ce qui a SERVI, pas ce qu'on croit configure."""
    routing_probe._sur_fin_inference(_Evenement({"model": "claude-sonnet-5"}))
    routing_probe._sur_echange_termine(_Evenement({"user_message": "x"}))
    assert _lignes(_journal_temporaire)[0]["modele"] == "claude-sonnet-5"


def test_l_etat_est_remis_a_zero_entre_deux_echanges(_journal_temporaire: Path) -> None:
    """⚠ Sans cela, un outil appele au premier echange serait attribue au second — et
    toute la statistique « proportion d'echanges avec outil » serait fausse a la hausse.
    """
    routing_probe._sur_debut_outil(_Evenement({"tool": "avalon_status"}))
    routing_probe._sur_echange_termine(_Evenement({"user_message": "premier"}))
    routing_probe._sur_echange_termine(_Evenement({"user_message": "second"}))

    lignes = _lignes(_journal_temporaire)
    assert lignes[0]["outil_appele"] is True
    assert lignes[1]["outil_appele"] is False


# ── Ce que la sonde ne doit JAMAIS faire ───────────────────────────────────────────


def test_aucun_contenu_de_conversation_n_est_ecrit(_journal_temporaire: Path) -> None:
    """⚠ INVARIANT DE CONFIDENTIALITE. Ce journal vit sur une VM exposee, et Ava parle
    de la maison, de la presence des personnes, de l'infrastructure. On garde la
    LONGUEUR de la question, jamais la question.
    """
    secret = "Annie est-elle seule a la maison ce soir ?"
    routing_probe._sur_echange_termine(_Evenement({"user_message": secret}))

    brut = _journal_temporaire.read_text(encoding="utf-8")
    assert secret not in brut
    assert "Annie" not in brut
    assert json.loads(brut)["longueur_question"] == len(secret)


def test_une_ecriture_impossible_ne_leve_pas(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ Une sonde qui fait tomber le service qu'elle observe est pire que pas de sonde.

    L'EventBus appelle ses abonnes de façon SYNCHRONE dans le thread qui publie : une
    exception ici remonterait au milieu d'une conversation.
    """
    monkeypatch.setattr(
        routing_probe, "CHEMIN", Path("/proc/interdit/impossible.jsonl")
    )
    routing_probe._sur_echange_termine(
        _Evenement({"user_message": "x"})
    )  # ne doit pas lever


def test_une_complexite_indisponible_vaut_None_pas_zero(
    _journal_temporaire: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ `0.0` se lirait comme « question triviale » et fausserait la statistique dans le
    sens meme qui nous interesse. Une mesure absente doit rester absente.
    """
    monkeypatch.setattr(routing_probe, "_score_complexite", lambda _: None)
    routing_probe._sur_echange_termine(_Evenement({"user_message": "x"}))
    assert _lignes(_journal_temporaire)[0]["complexite"] is None


def test_brancher_ne_leve_pas_si_le_bus_est_incompatible() -> None:
    """Si une montee amont renomme les evenements, Ava doit continuer SANS sa sonde."""

    class _BusCasse:
        def subscribe(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("EventType introuvable")

    assert routing_probe.brancher(_BusCasse()) is False


def test_brancher_s_abonne_aux_trois_evenements() -> None:
    vus: list[Any] = []

    class _Bus:
        def subscribe(self, type_evt: Any, _cb: Any) -> None:
            vus.append(getattr(type_evt, "value", str(type_evt)))

    assert routing_probe.brancher(_Bus()) is True
    assert set(vus) == {"tool_call_start", "inference_end", "chat_exchange_completed"}


# ══ Champs REELS de l'amont — corriges apres l'audit du 2026-08-04 ════════════════
# ⚠ Ces trois tests utilisent les noms de champs EXACTS que publie OpenJarvis, relevés
#   dans son code (`memory/service.py` et `agents/_stubs.py`). La sonde en cherchait
#   d'autres : elle tournait, s'annonçait active, et n'enregistrait que des `null`.
#   Une mesure fausse est pire qu'une mesure absente — on regle des seuils dessus.


def test_la_question_est_lue_dans_user_text(_journal_temporaire: Path) -> None:
    """⚠ `publish_completed_exchange` publie `user_text`, pas `user_message`.

    La sonde ecrivait `longueur_question: 0` sur CHAQUE echange : la correlation entre
    longueur de question et appel d'outil — sa seule raison d'etre — etait nulle par
    construction.
    """
    routing_probe._sur_echange_termine(
        _Evenement({"user_text": "Il fait quoi dehors ?"})
    )
    assert _lignes(_journal_temporaire)[0]["longueur_question"] == 21


def test_les_jetons_sont_lus_dans_le_sous_objet_usage(
    _journal_temporaire: Path,
) -> None:
    """⚠ L'amont publie `{"model": …, "usage": {"prompt_tokens": …}}` — la sonde lisait
    `prompt_tokens` a la RACINE, donc toujours `null`."""
    routing_probe._sur_fin_inference(
        _Evenement(
            {
                "model": "claude-sonnet-5",
                "usage": {"prompt_tokens": 97, "completion_tokens": 24},
            }
        )
    )
    routing_probe._sur_echange_termine(_Evenement({"user_text": "x"}))
    ligne = _lignes(_journal_temporaire)[0]
    assert ligne["jetons_entree"] == 97
    assert ligne["jetons_sortie"] == 24
    assert ligne["modele"] == "claude-sonnet-5"


def test_les_anciens_noms_restent_acceptes(_journal_temporaire: Path) -> None:
    """Repli volontaire : une montee amont peut renommer ces champs, et une sonde muette
    ne se signale pas. On accepte donc les deux formes."""
    routing_probe._sur_fin_inference(
        _Evenement({"prompt_tokens": 10, "completion_tokens": 2})
    )
    routing_probe._sur_echange_termine(_Evenement({"user_message": "ancienne forme"}))
    ligne = _lignes(_journal_temporaire)[0]
    assert ligne["jetons_entree"] == 10
    assert ligne["longueur_question"] == 14


def test_le_branchement_au_bus_serveur_est_idempotent() -> None:
    """⚠ `brancher_bus_serveur()` patche `EventBus.__init__`. Appele deux fois sans
    marqueur, il empilerait les abonnements — chaque echange serait alors enregistre en
    double, et toute statistique tiree du journal serait fausse d'un facteur 2."""
    assert routing_probe.brancher_bus_serveur() is True
    assert routing_probe.brancher_bus_serveur() is True
    from openjarvis.core.events import EventBus

    bus = EventBus()
    abonnes = {getattr(t, "value", str(t)): len(c) for t, c in bus._subscribers.items()}
    assert abonnes.get("chat_exchange_completed") == 1, abonnes


# ══ Isolation entre echanges concurrents — corrige le 2026-08-04 ══════════════════


def test_deux_echanges_qui_se_CHEVAUCHENT_ne_melangent_pas_leurs_mesures(
    _journal_temporaire: Path,
) -> None:
    """⚠ LE DEFAUT LE PLUS PERNICIEUX DE CETTE SONDE.

    `_courant` etait un unique dict de module. Deux echanges concurrents — A appelle
    `home_assistant` et consomme 3 200 jetons, B n'appelle aucun outil — donnaient :
    B termine le premier et **emporte les outils et les jetons de A**, puis `clear()` ;
    A sortait ensuite avec `outil_appele: false` et `jetons_entree: null`.

    Ce n'est pas du bruit, c'est une CORRELATION INVERSEE : `outil_appele` est le
    signal sur lequel se regleront les seuils Haiku/Sonnet/Opus. On reglerait le
    routage a l'envers, avec des mesures d'apparence parfaitement propre.

    Le scenario est reproduit ici en deux threads, chacun jouant la sequence complete
    d'un echange, entrelaces par des barrieres pour forcer le chevauchement.
    """
    import threading

    barriere = threading.Barrier(2)

    def echange_avec_outil() -> None:
        routing_probe._sur_debut_outil(_Evenement({"tool": "home_assistant"}))
        routing_probe._sur_fin_inference(
            _Evenement({"model": "claude-sonnet-5", "usage": {"prompt_tokens": 3200}})
        )
        barriere.wait()  # B publie sa fin AVANT nous
        barriere.wait()
        routing_probe._sur_echange_termine(_Evenement({"user_text": "avec outil"}))

    def echange_sans_outil() -> None:
        routing_probe._sur_fin_inference(
            _Evenement({"model": "claude-haiku-4-5", "usage": {"prompt_tokens": 90}})
        )
        barriere.wait()
        routing_probe._sur_echange_termine(_Evenement({"user_text": "sans"}))
        barriere.wait()

    fils = [
        threading.Thread(target=echange_avec_outil),
        threading.Thread(target=echange_sans_outil),
    ]
    for f in fils:
        f.start()
    for f in fils:
        f.join()

    lignes = {
        ligne["longueur_question"]: ligne for ligne in _lignes(_journal_temporaire)
    }
    avec, sans = lignes[10], lignes[4]
    assert avec["outils"] == ["home_assistant"], "l'echange A a perdu son outil"
    assert avec["jetons_entree"] == 3200
    assert avec["modele"] == "claude-sonnet-5"
    assert sans["outils"] == [], "l'echange B a herite de l'outil de A"
    assert sans["jetons_entree"] == 90
    assert sans["modele"] == "claude-haiku-4-5"


def test_l_etat_d_un_echange_est_LIBERE_a_sa_fin(_journal_temporaire: Path) -> None:
    """Sinon la sonde ferait grossir la memoire du processus qu'elle observe — et une
    sonde qui degrade son hote est pire que pas de sonde."""
    routing_probe._sur_debut_outil(_Evenement({"tool": "x"}))
    assert routing_probe._etats
    routing_probe._sur_echange_termine(_Evenement({"user_text": "fini"}))
    assert not routing_probe._etats


def test_les_echanges_JAMAIS_TERMINES_ne_fuient_pas(_journal_temporaire: Path) -> None:
    """⚠ Un echange interrompu (exception pendant l'inference, client qui raccroche) ne
    publie jamais son evenement de fin : son entree resterait indefiniment. La borne
    purge les plus anciennes."""
    for i in range(routing_probe._ETATS_MAX * 3):
        routing_probe._etats[10_000 + i] = {"_ne_le": 0.0}
        if len(routing_probe._etats) > routing_probe._ETATS_MAX:
            routing_probe._etats.clear()  # garde-fou du test lui-meme
    routing_probe._etats.clear()
    import threading

    def un_echange_abandonne() -> None:
        routing_probe._sur_debut_outil(_Evenement({"tool": "abandonne"}))

    for _ in range(routing_probe._ETATS_MAX * 2):
        f = threading.Thread(target=un_echange_abandonne)
        f.start()
        f.join()
    assert len(routing_probe._etats) <= routing_probe._ETATS_MAX
