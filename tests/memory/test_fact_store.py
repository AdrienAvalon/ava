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
    assert store.add(
        "Utilise Ansible pour l'automatisation et Grafana pour la supervision"
    )
    assert store.add("Utilise Ansible pour l'automatisation") is False
    assert store.count() == 1


def test_un_fait_ANCIEN_devenu_redondant_SORT(tmp_path):
    """Le second sens, et il compte autant : quand un fait plus complet arrive, l'ancien
    n'a plus de raison d'occuper l'invite."""
    store = LocalFactStore(path=tmp_path / "f.jsonl")
    assert store.add("Utilise Ansible pour l'automatisation")
    assert store.add(
        "Utilise Ansible pour l'automatisation et Grafana pour la supervision"
    )
    restants = [f.text for f in store.list()]
    assert restants == [
        "Utilise Ansible pour l'automatisation et Grafana pour la supervision"
    ]


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


# ══ Peremption : marquer, jamais supprimer (decision admin 2026-08-06) ═════════════


def test_un_fait_perime_est_MARQUE_et_CONSERVE(tmp_path):
    """⚠ LA DEMANDE DE L'ADMIN, mot pour mot : « qu'elle sache que certains faits ne sont
    plus d'actualite mais garder quand meme en memoire ». Un fait perime dit ce qui ETAIT
    vrai, donc ce qui a change — le supprimer efface cette information."""
    store = LocalFactStore(tmp_path / "facts.jsonl")
    store.add("La camera du salon est une Imilab")
    assert store.mark_stale(
        "La camera du salon est une Imilab", "remplacee par une Reolink"
    )
    faits = store.list()
    assert len(faits) == 1, "le fait doit rester en memoire"
    assert faits[0].perime is True
    assert faits[0].perime_par == "remplacee par une Reolink"


def test_le_marquage_SURVIT_au_rechargement(tmp_path):
    """Le marquage ne vaut rien s'il ne franchit pas un redemarrage : il vit dans le
    JSONL, aux cotes du texte."""
    chemin = tmp_path / "facts.jsonl"
    store = LocalFactStore(chemin)
    store.add("Le NAS off-site est en append-only")
    store.mark_stale("Le NAS off-site est en append-only", "purge desormais possible")
    relu = LocalFactStore(chemin).list()
    assert relu[0].perime is True and relu[0].perime_par == "purge desormais possible"


def test_marquer_un_fait_INTROUVABLE_rend_False(tmp_path):
    store = LocalFactStore(tmp_path / "facts.jsonl")
    store.add("Un fait quelconque")
    assert store.mark_stale("un fait qui n'existe pas") is False


def test_le_marquage_atteint_une_REFORMULATION(tmp_path):
    """⚠ Exiger la chaine exacte rendrait la fonction inutilisable a la main : on compare
    sur la meme empreinte que le dedoublonnage."""
    store = LocalFactStore(tmp_path / "facts.jsonl")
    store.add("L'utilisateur parle francais")
    assert store.mark_stale("parle francais") is True


def test_un_ancien_fichier_SANS_les_champs_se_relit(tmp_path):
    """Retrocompatibilite : les 168 lignes deja ecrites n'ont ni `perime_le` ni
    `perime_par`. Le defaut vaut « courant », donc aucune migration n'est requise."""
    chemin = tmp_path / "facts.jsonl"
    chemin.write_text(
        json.dumps({"text": "fait ancien", "source": "auto", "created_at": 1.0}) + "\n",
        encoding="utf-8",
    )
    faits = LocalFactStore(chemin).list()
    assert len(faits) == 1 and faits[0].perime is False


# ══ Eviction : ce qui part quand la memoire est pleine ════════════════════════════


def test_l_eviction_sacrifie_les_PERIMES_avant_les_courants(tmp_path):
    """⚠ L'ancienne regle gardait les plus RECENTS, donc jetait les plus ANCIENS —
    c'est-a-dire ceux qui ont survecu le plus longtemps a la curation, donc les plus
    durables. Mesure du 2026-08-06 : ~75 faits/jour pour un plafond de 1000, premiere
    suppression vers le 17 aout. Onze jours."""
    store = LocalFactStore(tmp_path / "facts.jsonl", max_facts=3)
    store.add("le disjoncteur est derriere la porte verte")  # durable, le plus ancien
    store.add("ancienne camera du salon Imilab")
    store.mark_stale("ancienne camera du salon Imilab", "remplacee")
    store.add("les parents habitent en face")
    store.add("le chat s appelle Ficelle")  # depasse le plafond
    textes = [f.text for f in store.list()]
    assert "le disjoncteur est derriere la porte verte" in textes, (
        "le fait le plus ancien est aussi le plus durable : il ne doit pas partir en premier"
    )
    assert "ancienne camera du salon Imilab" not in textes


def test_supprimer_un_fait_COURANT_laisse_une_TRACE(tmp_path, caplog):
    """⚠ Une memoire qui se vide sans rien dire est un angle mort. Quand il ne reste plus
    de perime a sacrifier, la suppression d'un fait courant est journalisee en WARNING."""
    import logging

    store = LocalFactStore(tmp_path / "facts.jsonl", max_facts=2)
    store.add("premier fait durable")
    store.add("deuxieme fait sans rapport")
    with caplog.at_level(logging.WARNING, logger="openjarvis.memory.store"):
        store.add("troisieme sujet totalement different")
    assert any("COURANT" in r.message for r in caplog.records)


# ══ Accents : le motif est ecrit sans, le francais en porte ═══════════════════════


def test_un_etat_ACCENTUE_est_refuse_comme_son_equivalent_sans_accent(tmp_path):
    """⚠ LE DEFAUT MESURE LE 2026-08-06. `_PERISSABLE` est ecrit sans accents
    (`apres-midi`, `derniere`, `redemarrages`) et etait applique au texte BRUT, qui est du
    francais accentue. Trois motifs sur treize ne pouvaient donc jamais declencher.
    ⚠ Le mesurer sur les faits STOCKES est un piege : le filtre refuse a l'ecriture, donc
    les faits stockes sont les SURVIVANTS. Ce qui prouve le defaut, c'est le survivant
    accentue trouve en memoire : « ... etaient presents a la maison cet apres-midi »."""
    store = LocalFactStore(tmp_path / "facts.jsonl")
    assert (
        store.add(
            "Annie et Jean-Pierre étaient présents à la maison cet après-midi",
            source="auto",
        )
        is False
    )
    assert (
        store.add(
            "Annie et Jean-Pierre etaient presents a la maison cet apres-midi",
            source="auto",
        )
        is False
    )
