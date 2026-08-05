"""Tests for the persistent fact store (openjarvis.memory.store)."""

from __future__ import annotations

import json

import pytest

from openjarvis.core.registry import FactStoreRegistry
from openjarvis.memory.store import LocalFactStore, create_fact_store


def test_add_and_list(tmp_path):
    store = LocalFactStore(tmp_path / "facts.jsonl")
    assert store.add("User prefers concise answers") is True
    assert store.add("User lives in Berlin") is True

    facts = store.list()
    assert [f.text for f in facts] == [
        "User prefers concise answers",
        "User lives in Berlin",
    ]
    assert store.count() == 2


def test_add_dedupes_case_insensitive(tmp_path):
    store = LocalFactStore(tmp_path / "facts.jsonl")
    assert store.add("Likes coffee") is True
    assert store.add("likes coffee") is False  # duplicate
    assert store.count() == 1


def test_add_skips_empty(tmp_path):
    store = LocalFactStore(tmp_path / "facts.jsonl")
    assert store.add("") is False
    assert store.add("   ") is False
    assert store.count() == 0


def test_add_many(tmp_path):
    store = LocalFactStore(tmp_path / "facts.jsonl")
    added = store.add_many(["a", "b", "a", "c"])  # one dupe
    assert added == 3
    assert store.count() == 3


def test_max_facts_evicts_oldest(tmp_path):
    store = LocalFactStore(tmp_path / "facts.jsonl", max_facts=2)
    store.add("first")
    store.add("second")
    store.add("third")
    facts = [f.text for f in store.list()]
    assert facts == ["second", "third"]  # oldest dropped


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "facts.jsonl"
    store = LocalFactStore(path)
    store.add("durable fact")

    reloaded = LocalFactStore(path)
    assert [f.text for f in reloaded.list()] == ["durable fact"]


def test_clear(tmp_path):
    path = tmp_path / "facts.jsonl"
    store = LocalFactStore(path)
    store.add("one")
    store.add("two")

    removed = store.clear()
    assert removed == 2
    assert store.count() == 0
    # A fresh instance also sees an empty store.
    assert LocalFactStore(path).count() == 0


def test_external_clear_does_not_resurrect_stale_facts(tmp_path):
    """A running store instance must not re-flush facts cleared elsewhere."""
    path = tmp_path / "facts.jsonl"
    running = LocalFactStore(path)
    cli = LocalFactStore(path)

    running.add("old fact")
    assert cli.clear() == 1

    running.add("new fact")

    assert [f.text for f in LocalFactStore(path).list()] == ["new fact"]


def test_load_skips_malformed_lines(tmp_path):
    path = tmp_path / "facts.jsonl"
    path.write_text(
        '{"text": "good fact"}\n'
        "this is not json\n"
        '{"text": ""}\n'  # empty text ignored
        '{"text": "another good"}\n',
        encoding="utf-8",
    )
    store = LocalFactStore(path)
    assert [f.text for f in store.list()] == ["good fact", "another good"]


def test_jsonl_round_trip_is_valid_json(tmp_path):
    path = tmp_path / "facts.jsonl"
    store = LocalFactStore(path)
    store.add("fact one", source="auto")

    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["text"] == "fact one"
    assert obj["source"] == "auto"
    assert "created_at" in obj


def test_create_fact_store_local(tmp_path):
    store = create_fact_store("local", path=tmp_path / "f.jsonl", max_facts=5)
    assert isinstance(store, LocalFactStore)


def test_create_fact_store_uses_fact_store_registry(tmp_path):
    class CustomFactStore(LocalFactStore):
        pass

    FactStoreRegistry.register_value("custom", CustomFactStore)

    store = create_fact_store("custom", path=tmp_path / "f.jsonl", max_facts=5)

    assert isinstance(store, CustomFactStore)


def test_create_fact_store_default_path_uses_openjarvis_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))

    store = create_fact_store("local")

    assert isinstance(store, LocalFactStore)
    assert store.path == tmp_path / "memory_facts.jsonl"


def test_create_fact_store_unknown_backend(tmp_path):
    with pytest.raises(ValueError):
        create_fact_store("cloud", path=tmp_path / "f.jsonl")


# ── Faits perissables et dedoublonnage normalise (2026-08-05) ──────────────────
#
# ⚠ Ces tests portent sur des faits REELLEMENT ECRITS dans `memory_facts.jsonl`, pas sur
#   des exemples inventes. C'est ce qui les rend difficiles a affaiblir par accident.


def test_un_ETAT_MESURABLE_n_est_pas_memorise(tmp_path):
    """⚠ LE DEFAUT LE PLUS COUTEUX : une memoire qui stocke de l'etat finit par
    CONTREDIRE les outils qui le mesurent. Observe le 2026-08-05 — Ava a ouvert une
    reponse par « j'avais en memoire une info comme quoi il n'y aurait que 2 copies »,
    sur un fait faux issu d'une premisse de test."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    perissables = [
        "Actuellement seulement 2 copies des sauvegardes sont disponibles",
        "Dernier backup effectue il y a 14 heures",
        "Infrastructure Avalon : score global 98/100",
        "Deploie regulierement des stacks (au moins 50 deploiements en 7 jours)",
        "Adrien et Aurelie ont quitte la maison a 11:59 cet apres-midi",
        "openjarvis.service en crash-loop : plus de 50 redemarrages en 24h",
    ]
    for t in perissables:
        assert store.add(t, source="auto") is False, f"aurait du etre refuse : {t}"
    assert store.count() == 0


def test_un_fait_DURABLE_passe(tmp_path):
    """Le jumeau, et il est indispensable : un filtre qui refuse tout serait pire que
    pas de filtre — la memoire cesserait d'exister sans que rien ne le signale."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    durables = [
        "Disjoncteur de la chaufferie des parents : derriere la porte verte",
        "Vit avec Annie, Jean-Pierre, Adrien et Aurelie",
        "Utilise Ansible pour l'automatisation",
        "A configure pve-02 pour s'eteindre volontairement pour economiser l'electricite",
    ]
    for t in durables:
        assert store.add(t, source="auto") is True, f"aurait du passer : {t}"
    assert store.count() == len(durables)


def test_une_demande_EXPLICITE_prime_sur_le_filtre(tmp_path):
    """⚠ Le filtre ne vise que l'extraction AUTOMATIQUE. Si l'utilisateur demande
    expressement de retenir quelque chose, c'est son choix — casser la memoire volontaire
    pour reparer la memoire subie serait un mauvais echange."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("Le backup a tourne il y a 2 heures", source="utilisateur") is True


def test_le_dedoublonnage_ignore_le_sujet_et_les_accents(tmp_path):
    """⚠ La comparaison de chaines EXACTES ne dedoublonnait rien : « Parle francais » et
    « L'utilisateur parle francais » coexistaient. Mesure du 2026-08-05 : ce seul fait
    etait present SEPT fois sur 105, et chacun est injecte dans l'invite systeme."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("Parle francais") is True
    assert store.add("L'utilisateur parle français") is False
    assert store.add("Utilisateur parle francais.") is False
    assert store.count() == 1


def test_le_dedoublonnage_ne_FUSIONNE_PAS_deux_faits_distincts(tmp_path):
    """Contre-test : une normalisation trop agressive ferait disparaitre de vrais faits,
    ce qui est bien pire qu'un doublon."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("Utilise Ansible pour l'automatisation") is True
    assert store.add("Utilise Grafana pour la supervision") is True
    assert store.count() == 2


# ── Curation a l'ecriture : paraphrases et faits devenus redondants (2026-08-05) ─


def test_un_fait_qui_n_APPORTE_RIEN_n_entre_pas(tmp_path):
    """⚠ Le dedoublonnage par empreinte ne voit que les reformulations de SUJET. Mesure du
    2026-08-05 : 7 faits sur 119 etaient strictement inclus dans un autre — des
    paraphrases qu'il laissait passer, et chacune est injectee dans l'invite systeme."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("Utilise Ansible pour l'automatisation et Grafana pour la supervision")
    assert store.add("Utilise Ansible pour l'automatisation") is False
    assert store.count() == 1


def test_un_fait_ANCIEN_devenu_redondant_SORT(tmp_path):
    """Le second sens, et il compte autant : quand un fait plus complet arrive, l'ancien
    n'a plus de raison d'occuper l'invite."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("Utilise Ansible pour l'automatisation")
    assert store.add("Utilise Ansible pour l'automatisation et Grafana pour la supervision")
    restants = [f.text for f in store.list()]
    assert restants == ["Utilise Ansible pour l'automatisation et Grafana pour la supervision"]


def test_deux_faits_PROCHES_mais_DISTINCTS_survivent(tmp_path):
    """⚠ LE CONTRE-TEST QUI PORTE TOUT LE RISQUE. On ne fusionne pas des faits « proches »,
    on retire des faits INCLUS. « Radiateur salon » et « Radiateur salle a manger » se
    recouvrent fortement et ne s'incluent PAS : les fusionner perdrait une piece de la
    maison — exactement le genre d'erreur qui envoie une consigne de chauffage au mauvais
    endroit."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("Le radiateur du salon est en mode hors-gel")
    assert store.add("Le radiateur de la salle a manger est en mode hors-gel")
    assert store.count() == 2


def test_a_information_egale_le_FRANCAIS_l_emporte(tmp_path):
    """⚠ Cas mesure : « L'utilisateur vit avec Annie, Jean-Pierre… » est strictement inclus
    dans « User has family members: Adrien, Aurelie… ». Garder « le plus informatif »
    garderait l'ANGLAIS, contre la regle d'ecriture en francais. A information equivalente,
    la langue tranche."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("User has family members Adrien Aurelie Annie and Jean Pierre")
    assert store.add("Vit avec Annie Jean Pierre Adrien Aurelie")
    restants = [f.text for f in store.list()]
    assert restants == ["Vit avec Annie Jean Pierre Adrien Aurelie"], restants
