"""Parcours shadow E2E du contrat cognitif durable d'Ava."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ava_extensions.memory.governed import (
    AuthorityCapability,
    AuthorityClaims,
    EvidenceKind,
    GovernedMemoryStore,
    MemoryKind,
    MemoryState,
    Projection,
    Sensitivity,
    Visibility,
)
from ava_extensions.memory.verify import verify


def _claims(
    actor: str,
    scope: str,
    *capabilities: AuthorityCapability,
    system_scopes: tuple[str, ...] = (),
) -> AuthorityClaims:
    return AuthorityClaims(
        actor=actor,
        authority_ref=f"shadow-e2e:{actor}",
        subject_scope=scope,
        system_scopes=system_scopes,
        capabilities=frozenset(capabilities),
    )


_IDENTITIES = {
    "sensor": _claims(
        "sensor:shadow-probe",
        "system:avalon",
        AuthorityCapability.RECORD_EVIDENCE,
    ),
    "codex": _claims(
        "model:codex",
        "system:avalon",
        AuthorityCapability.PROPOSE_MEMORY,
        AuthorityCapability.READ_INTERNAL,
    ),
    "claude": _claims(
        "model:claude",
        "system:avalon",
        AuthorityCapability.VALIDATE_MEMORY,
        AuthorityCapability.READ_INTERNAL,
    ),
    "review": _claims(
        "policy:shadow-review-v1",
        "system:avalon",
        AuthorityCapability.ACCEPT_MEMORY,
        AuthorityCapability.READ_INTERNAL,
    ),
    "other": _claims(
        "policy:other-system",
        "system:other",
        AuthorityCapability.READ_INTERNAL,
    ),
}


def _verify_credential(credential: object) -> AuthorityClaims:
    if not isinstance(credential, str) or credential not in _IDENTITIES:
        raise PermissionError("credential shadow inconnu")
    return _IDENTITIES[credential]


def _authority(store: GovernedMemoryStore, name: str):  # noqa: ANN202
    return store.authenticate(name)


def test_promotion_changement_de_moteur_projection_et_restore(
    tmp_path: Path,
) -> None:
    """Une connaissance traverse la chaîne sans devenir propriété d'un moteur."""

    store = GovernedMemoryStore(
        tmp_path / "runtime/cognition.db",
        authority_verifier=_verify_credential,
    )
    content_hash = "sha256:" + hashlib.sha256(b"avalon-shadow-ok").hexdigest()
    evidence = store.add_evidence(
        authority=_authority(store, "sensor"),
        kind=EvidenceKind.OBSERVATION,
        source_ref="shadow:probe:v1",
        subject_scope="system:avalon",
        content_hash=content_hash,
        trace_id="trace-shadow-1",
        correlation_id="e2e-shadow-1",
        observed_at=1_700_000_000.0,
    )
    candidate = store.propose(
        authority=_authority(store, "codex"),
        claim_key="availability:shadow-probe",
        kind=MemoryKind.SEMANTIC,
        statement="La sonde shadow Avalon est saine",
        subject_scope="system:avalon",
        evidence_ids=[evidence.evidence_id],
        sensitivity=Sensitivity.INTERNAL,
        visibility=Visibility.INTERNAL,
        projection=Projection.OBSIDIAN,
        confidence=0.95,
    )

    # La proposition de Codex puis sa validation par Claude restent invisibles.
    assert store.memories_for(_authority(store, "codex"), now=1_800_000_001.0) == []
    store.transition(
        candidate.memory_id,
        MemoryState.VALIDATED,
        authority=_authority(store, "claude"),
        reason="preuve et schéma shadow valides",
    )
    assert store.memories_for(_authority(store, "claude"), now=1_800_000_001.0) == []

    accepted = store.transition(
        candidate.memory_id,
        MemoryState.ACCEPTED,
        authority=_authority(store, "review"),
        reason="politique shadow v1 satisfaite",
    )
    assert accepted.memory_id == candidate.memory_id
    assert [
        memory.memory_id
        for memory in store.memories_for(
            _authority(store, "claude"), now=1_800_000_001.0
        )
    ] == [candidate.memory_id]
    assert store.memories_for(_authority(store, "other"), now=1_800_000_001.0) == []
    assert [
        memory.memory_id
        for memory in store.obsidian_projection(
            _authority(store, "review"), now=1_800_000_001.0
        )
    ] == [candidate.memory_id]

    backup = store.backup(tmp_path / "backup/cognition.db")
    verify(backup)
    restored = GovernedMemoryStore(
        backup,
        authority_verifier=_verify_credential,
    )
    restored.integrity_check()

    # Le moteur lecteur change de Codex à Claude ; identité, mémoire et preuve restent.
    assert [
        memory.memory_id
        for memory in restored.memories_for(
            _authority(restored, "claude"), now=1_800_000_001.0
        )
    ] == [candidate.memory_id]
    assert (
        restored.memories_for(_authority(restored, "other"), now=1_800_000_001.0) == []
    )
