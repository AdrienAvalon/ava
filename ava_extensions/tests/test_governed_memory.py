"""Contrats de la memoire gouvernee d'Ava."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ava_extensions.memory.governed import (
    AuthorityCapability,
    AuthorityClaims,
    EvidenceKind,
    GovernedMemoryStore,
    MemoryKind,
    MemoryState,
    Projection,
    Sensitivity,
    VerifiedPrincipal,
    Visibility,
)
from ava_extensions.memory.verify import verify

_MODEL_CAPABILITIES = frozenset(
    {
        AuthorityCapability.RECORD_EVIDENCE,
        AuthorityCapability.PROPOSE_MEMORY,
        AuthorityCapability.VALIDATE_MEMORY,
        AuthorityCapability.PROPOSE_CONTRADICTION,
    }
)
_HUMAN_GOVERNANCE_CAPABILITIES = frozenset(
    {
        AuthorityCapability.RECORD_EVIDENCE,
        AuthorityCapability.PROPOSE_MEMORY,
        AuthorityCapability.VALIDATE_MEMORY,
        AuthorityCapability.ACCEPT_MEMORY,
        AuthorityCapability.REVOKE_MEMORY,
        AuthorityCapability.APPROVE_CONTRADICTION,
        AuthorityCapability.RESOLVE_CONTRADICTION,
        AuthorityCapability.READ_PRIVATE,
        AuthorityCapability.READ_AUDIT,
    }
)


def _claims(
    actor: str,
    subject_scope: str,
    capabilities: frozenset[AuthorityCapability],
    *,
    household_scope: str = "",
    system_scopes: tuple[str, ...] = (),
) -> AuthorityClaims:
    return AuthorityClaims(
        actor=actor,
        authority_ref=f"test-adapter:{actor}",
        subject_scope=subject_scope,
        household_scope=household_scope,
        system_scopes=system_scopes,
        capabilities=capabilities,
    )


_TEST_IDENTITIES = {
    "synthetic:test-owner": _claims(
        "human:test-owner",
        "person:test-owner",
        _HUMAN_GOVERNANCE_CAPABILITIES,
        household_scope="household:avalon",
        system_scopes=("system:avalon",),
    ),
    "human:reviewer": _claims(
        "human:reviewer",
        "person:test-owner",
        _HUMAN_GOVERNANCE_CAPABILITIES,
        household_scope="household:avalon",
        system_scopes=("system:avalon",),
    ),
    "model:ava": _claims(
        "model:ava",
        "person:test-owner",
        _MODEL_CAPABILITIES,
        household_scope="household:avalon",
        system_scopes=("system:avalon",),
    ),
    "model:ava:system": _claims(
        "model:ava",
        "system:avalon",
        _MODEL_CAPABILITIES,
    ),
    "model:codex": _claims(
        "model:codex",
        "person:test-owner",
        _MODEL_CAPABILITIES,
        household_scope="household:avalon",
        system_scopes=("system:avalon",),
    ),
    "model:claude": _claims(
        "model:claude",
        "person:test-owner",
        _MODEL_CAPABILITIES,
        household_scope="household:avalon",
        system_scopes=("system:avalon",),
    ),
    "synthetic:test-guest:private": _claims(
        "human:test-guest",
        "person:test-guest",
        frozenset({AuthorityCapability.READ_PRIVATE}),
    ),
    "synthetic:test-guest:household": _claims(
        "human:test-guest",
        "person:test-guest",
        frozenset(
            {
                AuthorityCapability.READ_PRIVATE,
                AuthorityCapability.READ_HOUSEHOLD,
            }
        ),
        household_scope="household:avalon",
    ),
    "system:internal": _claims(
        "policy:internal-reader",
        "system:avalon",
        frozenset({AuthorityCapability.READ_INTERNAL}),
    ),
    "system:restricted": _claims(
        "policy:restricted-reader",
        "system:avalon",
        frozenset(
            {
                AuthorityCapability.READ_INTERNAL,
                AuthorityCapability.READ_RESTRICTED,
            }
        ),
    ),
    "system:other": _claims(
        "policy:other-reader",
        "system:other",
        frozenset({AuthorityCapability.READ_INTERNAL}),
    ),
}


def _verify_test_credential(credential: object) -> AuthorityClaims:
    if not isinstance(credential, str) or credential not in _TEST_IDENTITIES:
        raise PermissionError("credential de test inconnu")
    return _TEST_IDENTITIES[credential]


def _hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _hash_transition_for_test(
    memory_id: str,
    from_state: str,
    to_state: str,
    actor: str,
    reason: str,
    created_at: float,
) -> str:
    payload = json.dumps(
        [memory_id, from_state, to_state, actor, reason, created_at],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"tr_{hashlib.sha256(payload.encode()).hexdigest()}"


@pytest.fixture
def store(tmp_path: Path) -> GovernedMemoryStore:
    return GovernedMemoryStore(
        tmp_path / "private" / "cognition.db",
        authority_verifier=_verify_test_credential,
    )


def _authority(store: GovernedMemoryStore, credential: str) -> VerifiedPrincipal:
    return store.authenticate(credential)


def _evidence(
    store: GovernedMemoryStore,
    text: str = "declaration",
    *,
    kind: EvidenceKind = EvidenceKind.HUMAN_STATEMENT,
    actor: str = "synthetic:test-owner",
    scope: str = "person:test-owner",
    source_ref: str = "matrix:$event",
    observed_at: float = 42,
):
    return store.add_evidence(
        authority=_authority(store, actor),
        kind=kind,
        source_ref=source_ref,
        subject_scope=scope,
        content_hash=_hash(text),
        trace_id="trace-1",
        correlation_id="run-1",
        observed_at=observed_at,
    )


def _candidate(
    store: GovernedMemoryStore,
    statement: str = "Synthetic Test Owner prefere les reponses concises",
    *,
    kind: MemoryKind = MemoryKind.RELATIONSHIP,
    evidence=None,
    scope: str = "person:test-owner",
    sensitivity: Sensitivity = Sensitivity.PERSONAL,
    visibility: Visibility = Visibility.PRIVATE,
    projection: Projection = Projection.NONE,
    supersedes_id: str = "",
    claim_key: str = "preference:response-style",
):
    evidence = evidence or _evidence(store)
    return store.propose(
        authority=_authority(store, "model:ava"),
        claim_key=claim_key,
        kind=kind,
        statement=statement,
        subject_scope=scope,
        evidence_ids=[evidence.evidence_id],
        sensitivity=sensitivity,
        visibility=visibility,
        projection=projection,
        confidence=0.8,
        supersedes_id=supersedes_id,
    )


def _principal(store: GovernedMemoryStore, credential: str = "synthetic:test-owner"):
    return _authority(store, credential)


def test_un_contexte_dautorite_est_verifie_et_lie_au_magasin(
    store: GovernedMemoryStore, tmp_path: Path
) -> None:
    with pytest.raises(TypeError):
        VerifiedPrincipal(  # type: ignore[call-arg]
            actor="synthetic:test-owner",
            subject_scope="person:test-owner",
            authority_ref="auto-declare",
        )
    with pytest.raises(PermissionError, match="authentification refusee"):
        store.authenticate("credential-inconnu")

    other = GovernedMemoryStore(
        tmp_path / "other" / "cognition.db",
        authority_verifier=_verify_test_credential,
    )
    foreign_context = _authority(other, "synthetic:test-owner")
    with pytest.raises(PermissionError, match="autre magasin"):
        store.memories_for(foreign_context)


def test_une_sortie_de_modele_reste_candidate(store: GovernedMemoryStore) -> None:
    evidence = _evidence(
        store,
        kind=EvidenceKind.MODEL_PROPOSAL,
        actor="model:claude",
    )
    memory = _candidate(store, evidence=evidence)
    memory = store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="format valide",
    )

    with pytest.raises(ValueError, match="modele ou un test"):
        store.transition(
            memory.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "synthetic:test-owner"),
            reason="pas de preuve externe",
        )


def test_validation_independante_et_lecture_cloisonnee(
    store: GovernedMemoryStore,
) -> None:
    memory = _candidate(store)
    memory = store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    memory = store.transition(
        memory.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="confirme par Synthetic Test Owner",
    )

    assert [item.memory_id for item in store.memories_for(_principal(store))] == [
        memory.memory_id
    ]
    assert store.memories_for(_principal(store, "synthetic:test-guest:private")) == []


def test_un_modele_ne_choisit_pas_la_portee_dune_autre_personne(
    store: GovernedMemoryStore,
) -> None:
    with pytest.raises(PermissionError, match="portee demandee"):
        store.add_evidence(
            authority=_authority(store, "model:ava:system"),
            kind=EvidenceKind.MODEL_PROPOSAL,
            source_ref="trace:cross-scope",
            subject_scope="person:test-owner",
            content_hash=_hash("tentative inter-portee"),
        )

    evidence = _evidence(store)
    with pytest.raises(PermissionError, match="portee demandee"):
        store.propose(
            authority=_authority(store, "model:ava:system"),
            claim_key="preference:cross-scope",
            kind=MemoryKind.RELATIONSHIP,
            statement="Proposition hors portee",
            subject_scope="person:test-owner",
            evidence_ids=[evidence.evidence_id],
        )


def test_un_auteur_ne_valide_pas_sa_propre_proposition(
    store: GovernedMemoryStore,
) -> None:
    memory = _candidate(store)
    with pytest.raises(ValueError, match="independante"):
        store.transition(
            memory.memory_id,
            MemoryState.VALIDATED,
            authority=_authority(store, "model:ava"),
            reason="auto-validation interdite",
        )


def test_integrite_detecte_une_auto_validation_injectee(
    store: GovernedMemoryStore,
) -> None:
    memory = _candidate(store)
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="validation initiale",
    )
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT transition_id, from_state, to_state, reason, created_at "
            "FROM transitions WHERE memory_id = ?",
            (memory.memory_id,),
        ).fetchone()
        assert row is not None
        replacement_id = _hash_transition_for_test(
            memory.memory_id,
            row[1],
            row[2],
            "model:ava",
            row[3],
            row[4],
        )
        connection.execute(
            "UPDATE transitions SET transition_id = ?, actor = ? "
            "WHERE transition_id = ?",
            (replacement_id, "model:ava", row[0]),
        )

    with pytest.raises(RuntimeError, match="validation non independante"):
        store.integrity_check()


def test_un_acteur_ne_valide_pas_sa_propre_preuve(store: GovernedMemoryStore) -> None:
    memory = _candidate(store)
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="structure valide",
    )
    with pytest.raises(ValueError, match="independante"):
        store.transition(
            memory.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "synthetic:test-owner"),
            reason="auto validation",
        )


def test_une_contradiction_necrase_rien_et_bloque_acceptation(
    store: GovernedMemoryStore,
) -> None:
    left = _candidate(store, "Synthetic Test Owner prefere les reponses concises")
    second_evidence = _evidence(store, "declaration opposee")
    right = _candidate(
        store,
        "Synthetic Test Owner prefere les reponses detaillees",
        evidence=second_evidence,
    )
    conflict = store.contradict(
        left.memory_id,
        right.memory_id,
        authority=_authority(store, "model:ava"),
        reason="preferences incompatibles",
    )
    store.approve_contradiction(
        conflict,
        authority=_authority(store, "human:reviewer"),
        reason="conflit plausible",
    )
    store.transition(
        left.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )

    with pytest.raises(ValueError, match="contradiction"):
        store.transition(
            left.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "human:reviewer"),
            reason="pas encore arbitre",
        )

    store.resolve_contradiction(
        conflict,
        winner_memory_id=left.memory_id,
        authority=_authority(store, "synthetic:test-owner"),
        reason="preference confirmee",
    )
    accepted = store.transition(
        left.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="arbitrage attribue",
    )
    assert accepted.state is MemoryState.ACCEPTED
    store.integrity_check()


def test_integrite_detecte_la_reaffectation_dune_contradiction(
    store: GovernedMemoryStore,
) -> None:
    left = _candidate(store, "Synthetic Test Owner prefere une reponse concise")
    right_evidence = _evidence(store, "preference detaillee")
    right = _candidate(
        store,
        "Synthetic Test Owner prefere une reponse detaillee",
        evidence=right_evidence,
    )
    third_evidence = _evidence(store, "preference structuree")
    third = _candidate(
        store,
        "Synthetic Test Owner prefere une reponse structuree",
        evidence=third_evidence,
    )
    contradiction_id = store.contradict(
        left.memory_id,
        right.memory_id,
        authority=_authority(store, "model:ava"),
        reason="versions incompatibles",
    )
    store.approve_contradiction(
        contradiction_id,
        authority=_authority(store, "human:reviewer"),
        reason="conflit confirme",
    )
    reassigned = sorted((right.memory_id, third.memory_id))
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE contradictions
            SET left_memory_id = ?, right_memory_id = ?
            WHERE contradiction_id = ?
            """,
            (*reassigned, contradiction_id),
        )

    with pytest.raises(RuntimeError, match="contradiction non canonique"):
        store.integrity_check()


def test_peremption_temporelle_ecarte_un_fait(store: GovernedMemoryStore) -> None:
    evidence = _evidence(store)
    memory = store.propose(
        authority=_authority(store, "model:ava"),
        claim_key="fact:temporary",
        kind=MemoryKind.SEMANTIC,
        statement="Une information temporaire",
        subject_scope="person:test-owner",
        evidence_ids=[evidence.evidence_id],
        valid_from=10,
        valid_until=20,
    )
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    store.transition(
        memory.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="preuve confirmee",
    )
    assert store.memories_for(_principal(store), now=15)
    assert store.memories_for(_principal(store), now=21) == []


def test_une_preuve_future_ne_peut_pas_etre_acceptee(
    store: GovernedMemoryStore,
) -> None:
    evidence = _evidence(store, observed_at=4_102_444_800.0)
    memory = _candidate(store, evidence=evidence)
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="forme valide",
    )

    with pytest.raises(ValueError, match="preuve posterieure"):
        store.transition(
            memory.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "human:reviewer"),
            reason="ne doit pas anticiper l'observation",
        )


@pytest.mark.parametrize(
    "secret",
    [
        "password=abcdefghijk",
        "-----BEGIN PRIVATE KEY-----",
        "ENC[AES256_GCM,data:abc]",
        "eyJabcdefghijk.abcdefghijk.abcdefghijk",
    ],
)
def test_les_secrets_sont_refuses(store: GovernedMemoryStore, secret: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        _candidate(store, f"A retenir {secret}")


def test_obsidian_ne_recoit_que_laccepte_interne(store: GovernedMemoryStore) -> None:
    evidence = _evidence(store, scope="system:avalon")
    projected = _candidate(
        store,
        "Les evolutions d'Ava passent par un benchmark versionne",
        evidence=evidence,
        kind=MemoryKind.PROCEDURAL,
        scope="system:avalon",
        sensitivity=Sensitivity.INTERNAL,
        visibility=Visibility.INTERNAL,
        projection=Projection.OBSIDIAN,
    )
    store.transition(
        projected.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="contrat teste",
    )
    store.transition(
        projected.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="contrat approuve",
    )
    _candidate(store, "Souvenir prive")

    assert [
        item.memory_id
        for item in store.obsidian_projection(_authority(store, "system:internal"))
    ] == [projected.memory_id]
    assert store.obsidian_projection(_authority(store, "system:other")) == []
    with pytest.raises(ValueError, match="interne"):
        _candidate(store, "Donnee personnelle", projection=Projection.OBSIDIAN)


def test_backup_est_reouvrable_et_integral(
    store: GovernedMemoryStore, tmp_path: Path
) -> None:
    memory = _candidate(store)
    destination = store.backup(tmp_path / "backup" / "cognition.db")
    assert {path.name for path in destination.parent.iterdir()} == {"cognition.db"}
    before = destination.read_bytes()
    file_mode = destination.stat().st_mode
    parent_mode = destination.parent.stat().st_mode
    sidecars = {path.name for path in destination.parent.iterdir()}
    verify(destination)
    assert destination.read_bytes() == before
    assert destination.stat().st_mode == file_mode
    assert destination.parent.stat().st_mode == parent_mode
    assert {path.name for path in destination.parent.iterdir()} == sidecars

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ava_extensions.memory.verify",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    restored = GovernedMemoryStore(destination)
    restored.integrity_check()
    with sqlite_rows(destination) as rows:
        assert rows == 1
    assert destination.stat().st_mode & 0o777 == 0o600
    assert memory.memory_id


def test_validateur_hors_ligne_refuse_sans_initialiser(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()
    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    for database in (empty, unrelated):
        before = database.read_bytes()
        file_mode = database.stat().st_mode
        parent_mode = database.parent.stat().st_mode
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            verify(database)
        assert database.read_bytes() == before
        assert database.stat().st_mode == file_mode
        assert database.parent.stat().st_mode == parent_mode
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()


def test_validateur_hors_ligne_refuse_tout_journal_adjacent(
    store: GovernedMemoryStore, tmp_path: Path
) -> None:
    backup = store.backup(tmp_path / "backup/cognition.db")
    journal = Path(f"{backup}-journal")
    journal.write_bytes(b"rollback-incomplet")

    with pytest.raises(ValueError, match="sidecar SQLite"):
        verify(backup)


class sqlite_rows:
    """Petit contexte de test sans exposer la connexion hors de sa duree de vie."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> int:
        import sqlite3

        with sqlite3.connect(self.path) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            )

    def __exit__(self, *_args) -> None:
        return None


def test_preuve_idempotente_et_journal_append_only(store: GovernedMemoryStore) -> None:
    first = _evidence(store)
    second = _evidence(store)
    assert first.evidence_id == second.evidence_id
    memory = _candidate(store, evidence=first)
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="premiere transition",
    )
    store.transition(
        memory.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="seconde transition",
    )
    assert [
        row["to_state"]
        for row in store.transitions_for(
            memory.memory_id,
            authority=_authority(store, "human:reviewer"),
        )
    ] == [
        "validated",
        "accepted",
    ]


def test_manifeste_logique_ne_contient_pas_la_preuve_brute(
    store: GovernedMemoryStore,
) -> None:
    evidence = _evidence(store, "phrase personnelle tres precise")
    serialized = json.dumps(
        evidence.__dict__
        if hasattr(evidence, "__dict__")
        else {
            "content_hash": evidence.content_hash,
            "source_ref": evidence.source_ref,
        }
    )
    assert "phrase personnelle" not in serialized


def test_un_modele_ne_peut_pas_se_declarer_source_humaine(
    store: GovernedMemoryStore,
) -> None:
    with pytest.raises(ValueError, match="exige un acteur human"):
        _evidence(store, actor="model:ava")


def test_une_preuve_ne_change_jamais_de_personne(store: GovernedMemoryStore) -> None:
    evidence = _evidence(store, scope="person:test-owner")
    with pytest.raises(PermissionError, match="portee demandee"):
        _candidate(store, evidence=evidence, scope="person:test-guest")


def test_la_lecture_exige_un_principal_et_ses_capacites(
    store: GovernedMemoryStore,
) -> None:
    private = _candidate(store)
    store.transition(
        private.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    store.transition(
        private.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="confirme",
    )
    household_evidence = _evidence(
        store,
        "foyer",
        scope="household:avalon",
        source_ref="matrix:$household",
    )
    household = _candidate(
        store,
        "Le foyer prefere une alerte avant maintenance",
        kind=MemoryKind.SEMANTIC,
        evidence=household_evidence,
        scope="household:avalon",
        sensitivity=Sensitivity.PERSONAL,
        visibility=Visibility.HOUSEHOLD,
    )
    store.transition(
        household.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    store.transition(
        household.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="confirme",
    )

    with pytest.raises(PermissionError, match="contexte d'autorite"):
        store.memories_for("person:test-owner")  # type: ignore[arg-type]
    assert [m.memory_id for m in store.memories_for(_principal(store))] == [
        private.memory_id
    ]
    visible = store.memories_for(_principal(store, "synthetic:test-guest:household"))
    assert [m.memory_id for m in visible] == [household.memory_id]


def test_une_memoire_restreinte_exige_une_capacite_dediee(
    store: GovernedMemoryStore,
) -> None:
    internal = _candidate(
        store,
        "Etat interne non sensible",
        kind=MemoryKind.SEMANTIC,
        scope="system:avalon",
        sensitivity=Sensitivity.INTERNAL,
        visibility=Visibility.INTERNAL,
        claim_key="infra:health",
        evidence=_evidence(
            store,
            "etat interne",
            scope="system:avalon",
            source_ref="tool:health",
        ),
    )
    restricted = _candidate(
        store,
        "Constat reserve aux operateurs habilites",
        kind=MemoryKind.SEMANTIC,
        scope="system:avalon",
        sensitivity=Sensitivity.RESTRICTED,
        visibility=Visibility.INTERNAL,
        claim_key="infra:restricted-finding",
        evidence=_evidence(
            store,
            "constat restreint",
            scope="system:avalon",
            source_ref="tool:restricted",
        ),
    )
    for memory in (internal, restricted):
        store.transition(
            memory.memory_id,
            MemoryState.VALIDATED,
            authority=_authority(store, "model:codex"),
            reason="preuve structuree",
        )
        store.transition(
            memory.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "human:reviewer"),
            reason="preuve confirmee",
        )

    assert [
        memory.memory_id
        for memory in store.memories_for(_authority(store, "system:internal"))
    ] == [internal.memory_id]
    assert {
        memory.memory_id
        for memory in store.memories_for(_authority(store, "system:restricted"))
    } == {internal.memory_id, restricted.memory_id}
    assert store.memories_for(_authority(store, "model:ava")) == []


def test_acceptation_est_independante_des_preuves_et_de_la_validation(
    store: GovernedMemoryStore,
) -> None:
    own = _evidence(store, actor="human:reviewer")
    other = _evidence(
        store,
        "confirmation",
        source_ref="matrix:$other",
        actor="synthetic:test-owner",
    )
    memory = store.propose(
        authority=_authority(store, "model:ava"),
        claim_key="preference:response-style",
        kind=MemoryKind.RELATIONSHIP,
        statement="Synthetic Test Owner prefere les reponses concises",
        subject_scope="person:test-owner",
        evidence_ids=[own.evidence_id, other.evidence_id],
        confidence=0.8,
    )
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="structure valide",
    )
    with pytest.raises(ValueError, match="toutes les preuves"):
        store.transition(
            memory.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "human:reviewer"),
            reason="conflit d'interet",
        )

    second = _candidate(store, "Synthetic Test Owner prefere une synthese initiale")
    store.transition(
        second.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "human:reviewer"),
        reason="structure valide",
    )
    with pytest.raises(ValueError, match="deux acteurs distincts"):
        store.transition(
            second.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "human:reviewer"),
            reason="auto promotion",
        )


def test_resolution_revoque_le_perdant_et_les_conflits_sont_masques(
    store: GovernedMemoryStore,
) -> None:
    left = _candidate(store, "Synthetic Test Owner prefere les reponses concises")
    right = _candidate(
        store,
        "Synthetic Test Owner prefere les reponses detaillees",
        evidence=_evidence(store, "oppose", source_ref="matrix:$opposite"),
    )
    for memory in (left, right):
        store.transition(
            memory.memory_id,
            MemoryState.VALIDATED,
            authority=_authority(store, "model:codex"),
            reason="preuve lisible",
        )
        store.transition(
            memory.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "human:reviewer"),
            reason="confirme avant conflit",
        )
    conflict = store.contradict(
        left.memory_id,
        right.memory_id,
        authority=_authority(store, "model:ava"),
        reason="incompatibles",
    )
    assert {memory.memory_id for memory in store.memories_for(_principal(store))} == {
        left.memory_id,
        right.memory_id,
    }
    store.approve_contradiction(
        conflict,
        authority=_authority(store, "synthetic:test-owner"),
        reason="conflit confirme",
    )
    assert store.memories_for(_principal(store)) == []

    store.resolve_contradiction(
        conflict,
        winner_memory_id=left.memory_id,
        authority=_authority(store, "human:reviewer"),
        reason="preference actuelle",
    )
    assert [m.memory_id for m in store.memories_for(_principal(store))] == [
        left.memory_id
    ]
    with pytest.raises(ValueError, match="transition interdite"):
        store.transition(
            right.memory_id,
            MemoryState.VALIDATED,
            authority=_authority(store, "model:codex"),
            reason="tentative de resurrection",
        )


def test_integrite_detecte_une_approbation_de_contradiction_alteree(
    store: GovernedMemoryStore,
) -> None:
    left = _candidate(store, "Synthetic Test Owner prefere les reponses concises")
    right = _candidate(
        store,
        "Synthetic Test Owner prefere les reponses detaillees",
        evidence=_evidence(store, "oppose", source_ref="matrix:$opposite"),
    )
    conflict = store.contradict(
        left.memory_id,
        right.memory_id,
        authority=_authority(store, "model:ava"),
        reason="incompatibles",
    )
    store.approve_contradiction(
        conflict,
        authority=_authority(store, "synthetic:test-owner"),
        reason="conflit confirme",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE contradiction_approvals SET actor = ? WHERE contradiction_id = ?",
            ("human:intrus", conflict),
        )

    with pytest.raises(RuntimeError, match="empreinte d'approbation"):
        store.integrity_check()


def test_integrite_detecte_une_resolution_de_contradiction_alteree(
    store: GovernedMemoryStore,
) -> None:
    left = _candidate(store, "Synthetic Test Owner prefere les reponses concises")
    right = _candidate(
        store,
        "Synthetic Test Owner prefere les reponses detaillees",
        evidence=_evidence(store, "oppose", source_ref="matrix:$opposite"),
    )
    for memory in (left, right):
        store.transition(
            memory.memory_id,
            MemoryState.VALIDATED,
            authority=_authority(store, "model:codex"),
            reason="preuve lisible",
        )
        store.transition(
            memory.memory_id,
            MemoryState.ACCEPTED,
            authority=_authority(store, "human:reviewer"),
            reason="confirme avant conflit",
        )
    conflict = store.contradict(
        left.memory_id,
        right.memory_id,
        authority=_authority(store, "model:ava"),
        reason="incompatibles",
    )
    store.approve_contradiction(
        conflict,
        authority=_authority(store, "synthetic:test-owner"),
        reason="conflit confirme",
    )
    store.resolve_contradiction(
        conflict,
        winner_memory_id=left.memory_id,
        authority=_authority(store, "human:reviewer"),
        reason="preference actuelle",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE contradiction_resolutions SET reason = ? "
            "WHERE contradiction_id = ?",
            ("decision alteree", conflict),
        )

    with pytest.raises(RuntimeError, match="empreinte de resolution"):
        store.integrity_check()


def test_supersession_est_atomique_et_versionnee(store: GovernedMemoryStore) -> None:
    old = _candidate(store, "Synthetic Test Owner prefere les reponses concises")
    store.transition(
        old.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    store.transition(
        old.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="confirme",
    )
    new = _candidate(
        store,
        "Synthetic Test Owner prefere une synthese concise puis les details",
        evidence=_evidence(store, "mise a jour", source_ref="matrix:$new"),
        supersedes_id=old.memory_id,
    )
    store.transition(
        new.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    store.transition(
        new.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="preference mise a jour",
    )

    assert [m.memory_id for m in store.memories_for(_principal(store))] == [
        new.memory_id
    ]
    assert [
        row["to_state"]
        for row in store.transitions_for(
            old.memory_id,
            authority=_authority(store, "human:reviewer"),
        )
    ][-1] == "superseded"
    store.integrity_check()


def test_claim_key_borne_contradictions_et_supersessions(
    store: GovernedMemoryStore,
) -> None:
    response_style = _candidate(
        store,
        "Synthetic Test Owner prefere les reponses concises",
        claim_key="preference:response-style",
    )
    notification_style = _candidate(
        store,
        "Synthetic Test Owner prefere les notifications silencieuses",
        claim_key="preference:notification-style",
        evidence=_evidence(
            store,
            "notifications",
            source_ref="matrix:$notification-style",
        ),
    )
    with pytest.raises(ValueError, match="claim_key"):
        store.contradict(
            response_style.memory_id,
            notification_style.memory_id,
            authority=_authority(store, "model:ava"),
            reason="faux rapprochement",
        )

    store.transition(
        response_style.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    store.transition(
        response_style.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "human:reviewer"),
        reason="confirme",
    )
    with pytest.raises(ValueError, match="claim_key"):
        _candidate(
            store,
            "Synthetic Test Owner prefere etre prevenu avant une maintenance",
            claim_key="preference:maintenance-warning",
            evidence=_evidence(
                store,
                "maintenance",
                source_ref="matrix:$maintenance-warning",
            ),
            supersedes_id=response_style.memory_id,
        )


def test_une_affirmation_revoquee_peut_revenir_avec_une_nouvelle_preuve(
    store: GovernedMemoryStore,
) -> None:
    first = _candidate(store)
    store.transition(
        first.memory_id,
        MemoryState.REVOKED,
        authority=_authority(store, "synthetic:test-owner"),
        reason="information retiree",
    )
    second = _candidate(
        store,
        evidence=_evidence(store, "reconfirmee", source_ref="matrix:$later"),
    )
    assert first.semantic_key == second.semantic_key
    assert first.memory_id != second.memory_id
    assert second.state is MemoryState.CANDIDATE


def test_une_preuve_rejouee_retourne_la_date_reellement_persistee(
    store: GovernedMemoryStore,
) -> None:
    first = _evidence(store, observed_at=42)
    replay = _evidence(store, observed_at=84)
    recurrence = _evidence(store, observed_at=84, source_ref="matrix:$new-event")

    assert replay.evidence_id == first.evidence_id
    assert replay.observed_at == 42
    assert recurrence.evidence_id != first.evidence_id
    assert recurrence.observed_at == 84


def test_deux_moteurs_ne_dupliquent_pas_le_meme_candidat(
    store: GovernedMemoryStore,
) -> None:
    evidence = _evidence(store)
    first = _candidate(store, evidence=evidence)

    with pytest.raises(ValueError, match="proposed_by"):
        store.propose(
            authority=_authority(store, "model:claude"),
            claim_key="preference:response-style",
            kind=MemoryKind.RELATIONSHIP,
            statement="Synthetic Test Owner prefere les reponses concises",
            subject_scope="person:test-owner",
            evidence_ids=[evidence.evidence_id],
            sensitivity=Sensitivity.PERSONAL,
            visibility=Visibility.PRIVATE,
            confidence=0.8,
        )

    assert first.proposed_by == "model:ava"


def test_identite_de_version_inclut_confidence_et_validite(
    store: GovernedMemoryStore,
) -> None:
    evidence = _evidence(store, observed_at=42)

    def propose(*, confidence: float, valid_from: float | None = None):
        return store.propose(
            authority=_authority(store, "model:ava"),
            claim_key="preference:response-style",
            kind=MemoryKind.RELATIONSHIP,
            statement="Synthetic Test Owner prefere les reponses concises",
            subject_scope="person:test-owner",
            evidence_ids=[evidence.evidence_id],
            confidence=confidence,
            valid_from=valid_from,
        )

    initial = propose(confidence=0.8)
    replay = propose(confidence=0.8)
    rescored = propose(confidence=0.9)
    shifted = propose(confidence=0.8, valid_from=43)

    assert replay.memory_id == initial.memory_id
    assert replay.created_at == initial.created_at
    assert initial.valid_from == 42
    assert rescored.memory_id != initial.memory_id
    assert rescored.confidence == 0.9
    assert shifted.memory_id != initial.memory_id
    assert shifted.valid_from == 43


def test_une_version_future_est_refusee_sans_ddl(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta "
            "(singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_meta VALUES (1, 99)")
        connection.execute("CREATE TABLE sentinel (value TEXT)")
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="99"):
        GovernedMemoryStore(path)

    assert path.read_bytes() == before


def test_un_schema_v1_exige_une_migration_explicite_sans_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta "
            "(singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_meta VALUES (1, 1)")
        connection.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY)")
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="migration explicite"):
        GovernedMemoryStore(path)

    assert path.read_bytes() == before


def test_un_schema_v2_partiel_est_refuse(store: GovernedMemoryStore) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP INDEX memories_claim")

    with pytest.raises(RuntimeError, match="(?:DDL du schema|index v2) .*invalide"):
        GovernedMemoryStore(store.path)


def test_un_trigger_dauto_promotion_est_refuse(
    store: GovernedMemoryStore, tmp_path: Path
) -> None:
    backup = store.backup(tmp_path / "backup/cognition.db")
    with sqlite3.connect(backup) as connection:
        connection.execute(
            """
            CREATE TRIGGER auto_accept AFTER INSERT ON memories
            BEGIN
                UPDATE memories SET state = 'accepted'
                WHERE memory_id = NEW.memory_id;
            END
            """
        )

    with pytest.raises(RuntimeError, match="trigger ou vue"):
        verify(backup)


def test_un_ddl_sans_contrainte_est_refuse(
    store: GovernedMemoryStore, tmp_path: Path
) -> None:
    backup = store.backup(tmp_path / "backup/cognition.db")
    with sqlite3.connect(backup) as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
        ).fetchone()[0]
        altered = original.replace(" CHECK (confidence >= 0 AND confidence <= 1)", "")
        assert altered != original
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? "
            "WHERE type = 'table' AND name = 'memories'",
            (altered,),
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(RuntimeError, match="DDL du schema"):
        verify(backup)


def test_integrite_logique_detecte_un_etat_sans_journal(
    store: GovernedMemoryStore,
) -> None:
    memory = _candidate(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE memories SET state = ? WHERE memory_id = ?",
            (MemoryState.ACCEPTED.value, memory.memory_id),
        )

    with pytest.raises(RuntimeError, match="etat et journal divergent"):
        store.integrity_check()


def test_integrite_rejoue_la_politique_dacceptation(
    store: GovernedMemoryStore,
) -> None:
    from ava_extensions.memory import governed

    evidence = _evidence(
        store,
        kind=EvidenceKind.MODEL_PROPOSAL,
        actor="model:claude",
    )
    memory = _candidate(store, evidence=evidence)
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="forme valide",
    )
    actor = "policy:review"
    reason = "transition SQL pourtant canonique"
    created_at = 1_800_000_000.0
    transition_id = governed._canonical_hash(
        "tr",
        (
            memory.memory_id,
            MemoryState.VALIDATED.value,
            MemoryState.ACCEPTED.value,
            actor,
            reason,
            created_at,
        ),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO transitions(
                transition_id, memory_id, from_state, to_state, actor,
                reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                memory.memory_id,
                MemoryState.VALIDATED.value,
                MemoryState.ACCEPTED.value,
                actor,
                reason,
                created_at,
            ),
        )
        connection.execute(
            "UPDATE memories SET state = ? WHERE memory_id = ?",
            (MemoryState.ACCEPTED.value, memory.memory_id),
        )

    with pytest.raises(RuntimeError, match="modele ou un test"):
        store.integrity_check()


def test_integrite_detecte_une_memoire_posterieure_a_son_journal(
    store: GovernedMemoryStore,
) -> None:
    memory = _candidate(store)
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE memories
            SET created_at = (
                SELECT MAX(created_at) + 3600 FROM transitions
                WHERE memory_id = ?
            )
            WHERE memory_id = ?
            """,
            (memory.memory_id, memory.memory_id),
        )

    with pytest.raises(RuntimeError, match="transition anterieure"):
        store.integrity_check()


def test_integrite_logique_detecte_une_memoire_sans_preuve(
    store: GovernedMemoryStore,
) -> None:
    memory = _candidate(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM memory_evidence WHERE memory_id = ?",
            (memory.memory_id,),
        )

    with pytest.raises(RuntimeError, match="aucune preuve"):
        store.integrity_check()


def test_integrite_detecte_lalteration_de_la_date_observee(
    store: GovernedMemoryStore,
) -> None:
    memory = _candidate(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE evidence SET observed_at = 4102444800
            WHERE evidence_id IN (
                SELECT evidence_id FROM memory_evidence WHERE memory_id = ?
            )
            """,
            (memory.memory_id,),
        )

    with pytest.raises(RuntimeError, match="empreinte d'enregistrement"):
        store.integrity_check()


def test_backup_refuse_un_ecrasement_et_copie_toutes_les_tables(
    store: GovernedMemoryStore, tmp_path: Path
) -> None:
    memory = _candidate(store)
    store.transition(
        memory.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "model:codex"),
        reason="preuve lisible",
    )
    destination = store.backup(tmp_path / "backup" / "cognition.db")
    with pytest.raises(FileExistsError):
        store.backup(destination)
    with sqlite3.connect(destination) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("evidence", "memories", "memory_evidence", "transitions")
        }
    assert counts == {
        "evidence": 1,
        "memories": 1,
        "memory_evidence": 1,
        "transitions": 1,
    }
