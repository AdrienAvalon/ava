"""Ledger SQLite pour la memoire attribuee et reversible d'Ava.

Ce module ne remplace pas encore le magasin historique. Il fournit le contrat de la
migration en mode observation : preuves immuables, souvenirs candidats, transitions
append-only, portees de lecture et projection Obsidian expurgee.

Deux invariants dominent le reste :

* une sortie de modele n'est jamais sa propre preuve ;
* une evolution ne reecrit jamais silencieusement l'histoire qu'elle remplace.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 2
_MAX_STATEMENT_CHARS = 2_000
_SCOPE = re.compile(r"^(?:person|household|system|task):[a-z0-9][a-z0-9_.:-]{0,127}$")
_CLAIM_KEY = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,127}$")
_SECRET = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bAGE-SECRET-KEY-1[A-Z0-9]+"
    r"|\bENC\[AES256_GCM,"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s<]{8,})",
    re.IGNORECASE,
)

_SCHEMA_SQL = """
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 2)
);
CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject_scope TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    observed_at REAL NOT NULL,
    created_at REAL NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    semantic_key TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    subject_scope TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    visibility TEXT NOT NULL,
    projection TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    valid_from REAL NOT NULL,
    valid_until REAL NOT NULL,
    supersedes_id TEXT NOT NULL DEFAULT '',
    proposed_by TEXT NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE TABLE memory_evidence (
    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    PRIMARY KEY (memory_id, evidence_id)
);
CREATE TABLE transitions (
    transition_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE contradictions (
    contradiction_id TEXT PRIMARY KEY,
    left_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    right_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE contradiction_approvals (
    contradiction_id TEXT PRIMARY KEY REFERENCES contradictions(contradiction_id),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE TABLE contradiction_resolutions (
    contradiction_id TEXT PRIMARY KEY REFERENCES contradictions(contradiction_id),
    winner_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE INDEX memories_scope_state
    ON memories(subject_scope, state, created_at);
CREATE INDEX memories_claim
    ON memories(subject_scope, kind, claim_key, state);
CREATE INDEX evidence_correlation
    ON evidence(correlation_id, trace_id);
INSERT INTO schema_meta(singleton, version) VALUES (1, 2);
"""


class EvidenceKind(StrEnum):
    HUMAN_STATEMENT = "human_statement"
    OBSERVATION = "observation"
    TOOL_RESULT = "tool_result"
    TEST_RESULT = "test_result"
    MODEL_PROPOSAL = "model_proposal"
    ADVERSARIAL_PROBE = "adversarial_probe"
    SYNTHETIC = "synthetic"


class MemoryKind(StrEnum):
    RELATIONSHIP = "relationship"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    REFLECTION = "reflection"


class MemoryState(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class Sensitivity(StrEnum):
    INTERNAL = "internal"
    PERSONAL = "personal"
    RESTRICTED = "restricted"


class Visibility(StrEnum):
    PRIVATE = "private"
    HOUSEHOLD = "household"
    INTERNAL = "internal"


class Projection(StrEnum):
    NONE = "none"
    OBSIDIAN = "obsidian"


class AuthorityCapability(StrEnum):
    """Capacites minimales emises par l'adaptateur d'authentification."""

    RECORD_EVIDENCE = "memory:evidence:record"
    PROPOSE_MEMORY = "memory:propose"
    VALIDATE_MEMORY = "memory:validate"
    ACCEPT_MEMORY = "memory:accept"
    REVOKE_MEMORY = "memory:revoke"
    PROPOSE_CONTRADICTION = "memory:contradiction:propose"
    APPROVE_CONTRADICTION = "memory:contradiction:approve"
    RESOLVE_CONTRADICTION = "memory:contradiction:resolve"
    READ_PRIVATE = "memory:read:private"
    READ_HOUSEHOLD = "memory:read:household"
    READ_INTERNAL = "memory:read:internal"
    READ_RESTRICTED = "memory:read:restricted"
    READ_AUDIT = "memory:read:audit"


TRUSTED_EVIDENCE = frozenset(
    {EvidenceKind.HUMAN_STATEMENT, EvidenceKind.OBSERVATION, EvidenceKind.TOOL_RESULT}
)

_EXPECTED_COLUMNS = {
    "schema_meta": ("singleton", "version"),
    "evidence": (
        "evidence_id",
        "kind",
        "source_ref",
        "actor",
        "subject_scope",
        "content_hash",
        "trace_id",
        "correlation_id",
        "observed_at",
        "created_at",
        "record_hash",
    ),
    "memories": (
        "memory_id",
        "semantic_key",
        "claim_key",
        "kind",
        "statement",
        "subject_scope",
        "sensitivity",
        "visibility",
        "projection",
        "confidence",
        "state",
        "created_at",
        "valid_from",
        "valid_until",
        "supersedes_id",
        "proposed_by",
        "record_hash",
    ),
    "memory_evidence": ("memory_id", "evidence_id"),
    "transitions": (
        "transition_id",
        "memory_id",
        "from_state",
        "to_state",
        "actor",
        "reason",
        "created_at",
    ),
    "contradictions": (
        "contradiction_id",
        "left_memory_id",
        "right_memory_id",
        "actor",
        "reason",
        "created_at",
    ),
    "contradiction_approvals": (
        "contradiction_id",
        "actor",
        "reason",
        "created_at",
        "record_hash",
    ),
    "contradiction_resolutions": (
        "contradiction_id",
        "winner_memory_id",
        "actor",
        "reason",
        "created_at",
        "record_hash",
    ),
}
_EXPECTED_INDEXES = {
    "memories_scope_state": ("subject_scope", "state", "created_at"),
    "memories_claim": ("subject_scope", "kind", "claim_key", "state"),
    "evidence_correlation": ("correlation_id", "trace_id"),
}
_EXPECTED_INDEX_TABLES = {
    "memories_scope_state": "memories",
    "memories_claim": "memories",
    "evidence_correlation": "evidence",
}
_INTEGER_COLUMNS = {("schema_meta", "singleton"), ("schema_meta", "version")}
_REAL_COLUMNS = {
    ("evidence", "observed_at"),
    ("evidence", "created_at"),
    ("memories", "confidence"),
    ("memories", "created_at"),
    ("memories", "valid_from"),
    ("memories", "valid_until"),
    ("transitions", "created_at"),
    ("contradictions", "created_at"),
    ("contradiction_approvals", "created_at"),
    ("contradiction_resolutions", "created_at"),
}
_PRIMARY_KEYS = {
    ("schema_meta", "singleton"): 1,
    ("evidence", "evidence_id"): 1,
    ("memories", "memory_id"): 1,
    ("memory_evidence", "memory_id"): 1,
    ("memory_evidence", "evidence_id"): 2,
    ("transitions", "transition_id"): 1,
    ("contradictions", "contradiction_id"): 1,
    ("contradiction_approvals", "contradiction_id"): 1,
    ("contradiction_resolutions", "contradiction_id"): 1,
}
_NULLABLE_PRIMARY_KEYS = {key for key in _PRIMARY_KEYS if key[0] != "memory_evidence"}
_EXPECTED_FOREIGN_KEYS = {
    "memory_evidence": {
        ("memory_id", "memories", "memory_id"),
        ("evidence_id", "evidence", "evidence_id"),
    },
    "transitions": {("memory_id", "memories", "memory_id")},
    "contradictions": {
        ("left_memory_id", "memories", "memory_id"),
        ("right_memory_id", "memories", "memory_id"),
    },
    "contradiction_approvals": {
        ("contradiction_id", "contradictions", "contradiction_id")
    },
    "contradiction_resolutions": {
        ("contradiction_id", "contradictions", "contradiction_id"),
        ("winner_memory_id", "memories", "memory_id"),
    },
}


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    kind: EvidenceKind
    source_ref: str
    actor: str
    subject_scope: str
    content_hash: str
    trace_id: str
    correlation_id: str
    observed_at: float


@dataclass(frozen=True, slots=True)
class Memory:
    memory_id: str
    semantic_key: str
    claim_key: str
    kind: MemoryKind
    statement: str
    subject_scope: str
    sensitivity: Sensitivity
    visibility: Visibility
    projection: Projection
    confidence: float
    state: MemoryState
    created_at: float
    valid_from: float
    valid_until: float
    supersedes_id: str
    proposed_by: str


@dataclass(frozen=True, slots=True)
class AuthorityClaims:
    """Claims retournes par un adaptateur qui a deja authentifie l'appelant."""

    actor: str
    authority_ref: str
    subject_scope: str
    household_scope: str = ""
    system_scopes: tuple[str, ...] = ()
    capabilities: frozenset[AuthorityCapability] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True, init=False)
class VerifiedPrincipal:
    """Contexte opaque emis par un magasin apres verification par son adaptateur."""

    actor: str
    subject_scope: str
    authority_ref: str
    _seal: object = field(repr=False, compare=False)
    household_scope: str = ""
    system_scopes: tuple[str, ...] = ()
    capabilities: frozenset[AuthorityCapability] = field(default_factory=frozenset)

    @classmethod
    def _issue(cls, claims: AuthorityClaims, seal: object) -> VerifiedPrincipal:
        actor = _clean(claims.actor, "actor")
        authority_ref = _clean(claims.authority_ref, "authority_ref", maximum=256)
        subject_scope = _scope(claims.subject_scope)
        household_scope = (
            _scope(claims.household_scope) if claims.household_scope else ""
        )
        if household_scope and not household_scope.startswith("household:"):
            raise ValueError("household_scope doit etre une portee household")
        system_scopes = tuple(
            dict.fromkeys(_scope(scope) for scope in claims.system_scopes)
        )
        if any(not scope.startswith("system:") for scope in system_scopes):
            raise ValueError("system_scopes ne contient que des portees system")
        capabilities = frozenset(
            AuthorityCapability(item) for item in claims.capabilities
        )
        if (
            AuthorityCapability.READ_RESTRICTED in capabilities
            and AuthorityCapability.READ_INTERNAL not in capabilities
        ):
            raise ValueError("READ_RESTRICTED exige READ_INTERNAL")
        if any(_SECRET.search(value) for value in (actor, authority_ref)):
            raise ValueError("les claims d'autorite contiennent un secret potentiel")
        principal = object.__new__(cls)
        object.__setattr__(principal, "actor", actor)
        object.__setattr__(principal, "subject_scope", subject_scope)
        object.__setattr__(principal, "authority_ref", authority_ref)
        object.__setattr__(principal, "household_scope", household_scope)
        object.__setattr__(principal, "system_scopes", system_scopes)
        object.__setattr__(principal, "capabilities", capabilities)
        object.__setattr__(principal, "_seal", seal)
        return principal


def _canonical_hash(prefix: str, values: Iterable[object]) -> str:
    payload = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()}"


def _schema_definitions(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Normalise le DDL persiste, contraintes comprises, pour comparaison stricte."""

    return {
        (str(row["type"]), str(row["name"])): " ".join(str(row["sql"]).split())
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') "
            "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
        )
    }


def _expected_schema_definitions() -> dict[tuple[str, str], str]:
    with sqlite3.connect(":memory:") as reference:
        reference.row_factory = sqlite3.Row
        reference.executescript(_SCHEMA_SQL)
        return _schema_definitions(reference)


def _clean(value: str, field: str, *, maximum: int = 256) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} est requis")
    if len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError(f"{field} est invalide")
    return result


def _scope(value: str) -> str:
    result = _clean(value, "subject_scope", maximum=128).lower()
    if not _SCOPE.fullmatch(result):
        raise ValueError("subject_scope doit etre une portee type:identifiant")
    return result


def _claim_key(value: str) -> str:
    result = _clean(value, "claim_key", maximum=128).casefold()
    if not _CLAIM_KEY.fullmatch(result):
        raise ValueError("claim_key doit etre un identifiant semantique stable")
    return result


def _statement(value: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > _MAX_STATEMENT_CHARS:
        raise ValueError("statement est vide ou trop long")
    if _SECRET.search(result):
        raise ValueError("un secret potentiel ne peut pas entrer dans la memoire")
    return result


def _optional(value: str, field: str, *, maximum: int = 128) -> str:
    result = str(value or "").strip()
    if len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError(f"{field} est invalide")
    return result


def _validate_evidence_actor(kind: EvidenceKind, actor: str) -> None:
    required_prefix = {
        EvidenceKind.HUMAN_STATEMENT: "human:",
        EvidenceKind.MODEL_PROPOSAL: "model:",
        EvidenceKind.SYNTHETIC: "synthetic:",
        EvidenceKind.TEST_RESULT: "test:",
        EvidenceKind.ADVERSARIAL_PROBE: "test:",
        EvidenceKind.TOOL_RESULT: "tool:",
        EvidenceKind.OBSERVATION: "sensor:",
    }[kind]
    if not actor.startswith(required_prefix):
        raise ValueError(
            f"une preuve {kind.value} exige un acteur {required_prefix}..."
        )


def _acceptance_actor(actor: str) -> None:
    if not actor.startswith(("human:", "policy:")):
        raise ValueError("seul un humain ou une politique deterministe peut accepter")


def _validate_memory_policy(
    kind: MemoryKind,
    subject_scope: str,
    sensitivity: Sensitivity,
    visibility: Visibility,
    projection: Projection,
) -> None:
    if projection is Projection.OBSIDIAN and (
        sensitivity is not Sensitivity.INTERNAL or visibility is not Visibility.INTERNAL
    ):
        raise ValueError(
            "seule une memoire interne non personnelle peut etre projetee dans Obsidian"
        )
    if sensitivity is Sensitivity.RESTRICTED and visibility is not Visibility.INTERNAL:
        raise ValueError("une memoire restreinte exige une audience interne")
    if sensitivity is Sensitivity.PERSONAL and visibility is Visibility.INTERNAL:
        raise ValueError("une memoire personnelle ne peut pas etre interne")
    if kind is MemoryKind.RELATIONSHIP and (
        not subject_scope.startswith("person:")
        or sensitivity is not Sensitivity.PERSONAL
        or visibility is not Visibility.PRIVATE
        or projection is not Projection.NONE
    ):
        raise ValueError("une memoire relationnelle reste personnelle et privee")
    if projection is Projection.OBSIDIAN and kind not in {
        MemoryKind.SEMANTIC,
        MemoryKind.PROCEDURAL,
    }:
        raise ValueError("seuls les faits semantiques ou procedures vont dans Obsidian")
    scope_kind = subject_scope.split(":", 1)[0]
    allowed_scope_kinds = {
        Visibility.PRIVATE: {"person", "task"},
        Visibility.HOUSEHOLD: {"household"},
        Visibility.INTERNAL: {"system", "task"},
    }
    if scope_kind not in allowed_scope_kinds[visibility]:
        raise ValueError(
            f"audience {visibility.value} incoherente avec {subject_scope}"
        )


class GovernedMemoryStore:
    """Magasin local transactionnel de preuves et souvenirs gouvernes."""

    def __init__(
        self,
        path: str | Path,
        *,
        authority_verifier: Callable[[object], AuthorityClaims] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self._authority_verifier = authority_verifier
        self._authority_seal = object()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        if self.path.is_symlink():
            raise ValueError("le ledger memoire ne peut pas etre un lien symbolique")
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self._initialize()

    def authenticate(self, credential: object) -> VerifiedPrincipal:
        """Verifie un credential opaque puis emet un contexte lie a ce magasin."""

        if self._authority_verifier is None:
            raise RuntimeError("aucun adaptateur d'authentification n'est configure")
        try:
            claims = self._authority_verifier(credential)
        except Exception as exc:
            raise PermissionError("authentification refusee") from exc
        if not isinstance(claims, AuthorityClaims):
            raise PermissionError("l'adaptateur a retourne des claims invalides")
        try:
            return VerifiedPrincipal._issue(claims, self._authority_seal)
        except (TypeError, ValueError) as exc:
            raise PermissionError(
                "l'adaptateur a retourne des claims invalides"
            ) from exc

    def _require_authority(
        self,
        authority: VerifiedPrincipal,
        capability: AuthorityCapability,
    ) -> VerifiedPrincipal:
        authority = self._require_issued_authority(authority)
        if capability not in authority.capabilities:
            raise PermissionError(f"capacite requise: {capability.value}")
        return authority

    def _require_issued_authority(
        self, authority: VerifiedPrincipal
    ) -> VerifiedPrincipal:
        if (
            not isinstance(authority, VerifiedPrincipal)
            or authority._seal is not self._authority_seal
        ):
            raise PermissionError(
                "contexte d'autorite absent ou emis par un autre magasin"
            )
        return authority

    @staticmethod
    def _require_write_scope(
        authority: VerifiedPrincipal,
        subject_scope: str,
    ) -> None:
        """Refuse any audience not granted by the authentication adapter."""

        allowed = {authority.subject_scope, *authority.system_scopes}
        if authority.household_scope:
            allowed.add(authority.household_scope)
        if subject_scope not in allowed:
            raise PermissionError(
                "la portee demandee n'est pas accordee a cette autorite"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
            if meta_exists is not None:
                try:
                    row = connection.execute(
                        "SELECT version FROM schema_meta WHERE singleton = 1"
                    ).fetchone()
                except sqlite3.DatabaseError as exc:
                    raise RuntimeError("schema_meta v2 invalide") from exc
                if row is None or int(row["version"]) != SCHEMA_VERSION:
                    version = "absente" if row is None else row["version"]
                    raise RuntimeError(
                        f"schema memoire non supporte: {version}; "
                        "migration explicite requise"
                    )
                self._assert_schema_shape(connection)
            else:
                other_tables = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if other_tables is not None:
                    raise RuntimeError("base memoire non vide sans version de schema")
                connection.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA_SQL}\nCOMMIT;")
                self._assert_schema_shape(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        os.chmod(self.path, 0o600)

    @staticmethod
    def _assert_schema_shape(connection: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != set(_EXPECTED_COLUMNS):
            raise RuntimeError("forme du schema memoire v2 invalide")
        executable_schema_objects = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')"
        ).fetchall()
        if executable_schema_objects:
            raise RuntimeError("trigger ou vue inattendu dans le schema memoire v2")
        if _schema_definitions(connection) != _expected_schema_definitions():
            raise RuntimeError("DDL du schema memoire v2 invalide")
        for table, expected_columns in _EXPECTED_COLUMNS.items():
            column_info = connection.execute(f"PRAGMA table_info({table})").fetchall()
            columns = tuple(row["name"] for row in column_info)
            if columns != expected_columns:
                raise RuntimeError(f"forme de table v2 invalide: {table}")
            for row in column_info:
                key = (table, row["name"])
                expected_type = (
                    "INTEGER"
                    if key in _INTEGER_COLUMNS
                    else "REAL"
                    if key in _REAL_COLUMNS
                    else "TEXT"
                )
                expected_primary_key = _PRIMARY_KEYS.get(key, 0)
                expected_not_null = int(key not in _NULLABLE_PRIMARY_KEYS)
                expected_default = (
                    "''" if key == ("memories", "supersedes_id") else None
                )
                actual = (
                    str(row["type"]).upper(),
                    int(row["notnull"]),
                    row["dflt_value"],
                    int(row["pk"]),
                )
                expected = (
                    expected_type,
                    expected_not_null,
                    expected_default,
                    expected_primary_key,
                )
                if actual != expected:
                    raise RuntimeError(
                        f"contrat de colonne v2 invalide: {table}.{row['name']}"
                    )
            foreign_keys = {
                (row["from"], row["table"], row["to"])
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            if foreign_keys != _EXPECTED_FOREIGN_KEYS.get(table, set()):
                raise RuntimeError(f"cles etrangeres v2 invalides: {table}")
        declared_indexes = {
            row["name"]: row["tbl_name"]
            for row in connection.execute(
                "SELECT name, tbl_name FROM sqlite_master "
                "WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        if declared_indexes != _EXPECTED_INDEX_TABLES:
            raise RuntimeError("ensemble d'index v2 invalide")
        for index, expected_columns in _EXPECTED_INDEXES.items():
            index_row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index,),
            ).fetchone()
            columns = tuple(
                row["name"] for row in connection.execute(f"PRAGMA index_info({index})")
            )
            if index_row is None or columns != expected_columns:
                raise RuntimeError(f"index v2 invalide: {index}")
            properties = {
                row["name"]: (
                    int(row["unique"]),
                    str(row["origin"]),
                    int(row["partial"]),
                )
                for row in connection.execute(
                    f"PRAGMA index_list({_EXPECTED_INDEX_TABLES[index]})"
                )
            }
            if properties.get(index) != (0, "c", 0):
                raise RuntimeError(f"proprietes d'index v2 invalides: {index}")

    def add_evidence(
        self,
        *,
        authority: VerifiedPrincipal,
        kind: EvidenceKind,
        source_ref: str,
        subject_scope: str,
        content_hash: str,
        trace_id: str = "",
        correlation_id: str = "",
        observed_at: float | None = None,
    ) -> Evidence:
        authority = self._require_authority(
            authority, AuthorityCapability.RECORD_EVIDENCE
        )
        kind = EvidenceKind(kind)
        source_ref = _clean(source_ref, "source_ref", maximum=512)
        actor = authority.actor
        _validate_evidence_actor(kind, actor)
        subject_scope = _scope(subject_scope)
        self._require_write_scope(authority, subject_scope)
        content_hash = _clean(content_hash, "content_hash", maximum=128).lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash):
            raise ValueError("content_hash doit etre un SHA-256 attribuable")
        trace_id = _optional(trace_id, "trace_id")
        correlation_id = _optional(correlation_id, "correlation_id")
        if any(
            _SECRET.search(value)
            for value in (source_ref, actor, trace_id, correlation_id)
            if value
        ):
            raise ValueError(
                "un secret potentiel ne peut pas entrer dans les metadonnees"
            )
        observed = float(observed_at if observed_at is not None else time.time())
        if not math.isfinite(observed):
            raise ValueError("observed_at doit etre fini")
        evidence_id = _canonical_hash(
            "ev",
            (
                kind.value,
                source_ref,
                actor,
                subject_scope,
                content_hash,
                trace_id,
                correlation_id,
            ),
        )
        created_at = time.time()
        record_hash = _canonical_hash("evrecord", (evidence_id, observed, created_at))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO evidence(
                    evidence_id, kind, source_ref, actor, subject_scope, content_hash,
                    trace_id, correlation_id, observed_at, created_at, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    kind.value,
                    source_ref,
                    actor,
                    subject_scope,
                    content_hash,
                    trace_id,
                    correlation_id,
                    observed,
                    created_at,
                    record_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        assert row is not None
        return Evidence(
            evidence_id=row["evidence_id"],
            kind=EvidenceKind(row["kind"]),
            source_ref=row["source_ref"],
            actor=row["actor"],
            subject_scope=row["subject_scope"],
            content_hash=row["content_hash"],
            trace_id=row["trace_id"],
            correlation_id=row["correlation_id"],
            observed_at=float(row["observed_at"]),
        )

    def propose(
        self,
        *,
        authority: VerifiedPrincipal,
        claim_key: str,
        kind: MemoryKind,
        statement: str,
        subject_scope: str,
        evidence_ids: Iterable[str],
        sensitivity: Sensitivity = Sensitivity.PERSONAL,
        visibility: Visibility = Visibility.PRIVATE,
        projection: Projection = Projection.NONE,
        confidence: float = 0.0,
        valid_from: float | None = None,
        valid_until: float = 0.0,
        supersedes_id: str = "",
    ) -> Memory:
        authority = self._require_authority(
            authority, AuthorityCapability.PROPOSE_MEMORY
        )
        kind = MemoryKind(kind)
        sensitivity = Sensitivity(sensitivity)
        visibility = Visibility(visibility)
        projection = Projection(projection)
        claim_key = _claim_key(claim_key)
        statement = _statement(statement)
        subject_scope = _scope(subject_scope)
        self._require_write_scope(authority, subject_scope)
        evidence_ids = tuple(
            dict.fromkeys(
                str(item).strip() for item in evidence_ids if str(item).strip()
            )
        )
        if not evidence_ids:
            raise ValueError("une memoire candidate exige au moins une preuve")
        _validate_memory_policy(
            kind, subject_scope, sensitivity, visibility, projection
        )
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence doit etre comprise entre 0 et 1")
        created_at = time.time()
        explicit_valid_from = float(valid_from) if valid_from is not None else None
        valid_until = float(valid_until)
        if not math.isfinite(valid_until):
            raise ValueError("valid_until doit etre fini")
        supersedes_id = _optional(supersedes_id, "supersedes_id")
        semantic_key = _canonical_hash(
            "concept",
            (kind.value, subject_scope, " ".join(statement.casefold().split())),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in evidence_ids)
            found = connection.execute(
                f"""SELECT evidence_id, subject_scope, observed_at FROM evidence
                    WHERE evidence_id IN ({placeholders})""",
                evidence_ids,
            ).fetchall()
            if {row["evidence_id"] for row in found} != set(evidence_ids):
                raise ValueError("preuve inconnue")
            if any(row["subject_scope"] != subject_scope for row in found):
                raise ValueError(
                    "la portee de chaque preuve doit correspondre au souvenir"
                )
            default_valid_from = max(float(row["observed_at"]) for row in found)
            resolved_valid_from = (
                explicit_valid_from
                if explicit_valid_from is not None
                else default_valid_from
            )
            if not math.isfinite(resolved_valid_from):
                raise ValueError("valid_from doit etre fini")
            if valid_until and valid_until <= resolved_valid_from:
                raise ValueError("valid_until doit etre posterieur a valid_from")
            memory_id = _canonical_hash(
                "memv",
                (
                    semantic_key,
                    claim_key,
                    sorted(evidence_ids),
                    sensitivity.value,
                    visibility.value,
                    projection.value,
                    confidence,
                    resolved_valid_from,
                    valid_until,
                    supersedes_id,
                ),
            )
            record_hash = _canonical_hash(
                "memrecord", (memory_id, created_at, authority.actor)
            )
            if supersedes_id == memory_id:
                raise ValueError("une version ne peut pas se remplacer elle-meme")
            if supersedes_id:
                predecessor = connection.execute(
                    "SELECT subject_scope, kind, claim_key, state FROM memories "
                    "WHERE memory_id = ?",
                    (supersedes_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("memoire remplacee inconnue")
                if (
                    predecessor["subject_scope"],
                    predecessor["kind"],
                    predecessor["claim_key"],
                ) != (subject_scope, kind.value, claim_key):
                    raise ValueError(
                        "une supersession exige la meme portee, le meme type "
                        "et la meme claim_key"
                    )
                if predecessor["state"] != MemoryState.ACCEPTED.value:
                    raise ValueError("seule une memoire acceptee peut etre remplacee")
            existing = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if existing is not None:
                immutable = {
                    "semantic_key": semantic_key,
                    "claim_key": claim_key,
                    "kind": kind.value,
                    "statement": statement,
                    "subject_scope": subject_scope,
                    "sensitivity": sensitivity.value,
                    "visibility": visibility.value,
                    "projection": projection.value,
                    "confidence": confidence,
                    "valid_from": resolved_valid_from,
                    "valid_until": valid_until,
                    "supersedes_id": supersedes_id,
                    "proposed_by": authority.actor,
                }
                mismatched = [
                    field
                    for field, expected in immutable.items()
                    if existing[field] != expected
                ]
                if mismatched:
                    raise ValueError(
                        "reproposition incompatible avec la version existante: "
                        + ", ".join(mismatched)
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO memories(
                    memory_id, semantic_key, claim_key, kind, statement, subject_scope,
                    sensitivity, visibility, projection, confidence, state, created_at,
                    valid_from, valid_until, supersedes_id, proposed_by, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    semantic_key,
                    claim_key,
                    kind.value,
                    statement,
                    subject_scope,
                    sensitivity.value,
                    visibility.value,
                    projection.value,
                    confidence,
                    MemoryState.CANDIDATE.value,
                    created_at,
                    resolved_valid_from,
                    valid_until,
                    supersedes_id,
                    authority.actor,
                    record_hash,
                ),
            )
            for evidence_id in evidence_ids:
                connection.execute(
                    """INSERT OR IGNORE INTO memory_evidence(memory_id, evidence_id)
                    VALUES (?, ?)""",
                    (memory_id, evidence_id),
                )
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        assert row is not None
        return self._memory(row)

    def transition(
        self,
        memory_id: str,
        to_state: MemoryState,
        *,
        authority: VerifiedPrincipal,
        reason: str,
    ) -> Memory:
        to_state = MemoryState(to_state)
        required_capability = {
            MemoryState.VALIDATED: AuthorityCapability.VALIDATE_MEMORY,
            MemoryState.ACCEPTED: AuthorityCapability.ACCEPT_MEMORY,
            MemoryState.REVOKED: AuthorityCapability.REVOKE_MEMORY,
        }.get(to_state)
        if required_capability is None:
            raise ValueError("cette transition est reservee au moteur de gouvernance")
        authority = self._require_authority(authority, required_capability)
        actor = authority.actor
        reason = _clean(reason, "reason", maximum=1_000)
        if _SECRET.search(actor) or _SECRET.search(reason):
            raise ValueError("un secret potentiel ne peut pas entrer dans le journal")
        if to_state is MemoryState.ACCEPTED:
            _acceptance_actor(actor)
        allowed = {
            MemoryState.CANDIDATE: {MemoryState.VALIDATED, MemoryState.REVOKED},
            MemoryState.VALIDATED: {MemoryState.ACCEPTED, MemoryState.REVOKED},
            MemoryState.ACCEPTED: {MemoryState.REVOKED},
            MemoryState.SUPERSEDED: set(),
            MemoryState.REVOKED: set(),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            self._require_write_scope(authority, str(row["subject_scope"]))
            current = MemoryState(row["state"])
            if to_state not in allowed[current]:
                raise ValueError(
                    f"transition interdite: {current.value} -> {to_state.value}"
                )
            if to_state is MemoryState.VALIDATED and actor == str(row["proposed_by"]):
                raise ValueError(
                    "la validation doit etre independante de l'auteur de la proposition"
                )
            terminal_transition = to_state in {
                MemoryState.SUPERSEDED,
                MemoryState.REVOKED,
            }
            if terminal_transition and self._has_approved_conflict(
                connection, memory_id
            ):
                raise ValueError(
                    "une contradiction approuvee doit etre resolue avant "
                    "une transition terminale"
                )
            created_at = time.time()
            if to_state is MemoryState.ACCEPTED:
                self._assert_acceptable(
                    connection,
                    memory_id,
                    actor,
                    at=created_at,
                )
            self._append_transition(
                connection,
                memory_id,
                current,
                to_state,
                actor,
                reason,
                created_at,
            )
            connection.execute(
                "UPDATE memories SET state = ? WHERE memory_id = ?",
                (to_state.value, memory_id),
            )
            predecessor_id = row["supersedes_id"]
            if to_state is MemoryState.ACCEPTED and predecessor_id:
                if self._has_approved_conflict(connection, predecessor_id):
                    raise ValueError(
                        "la memoire remplacee a une contradiction approuvee non resolue"
                    )
                predecessor = connection.execute(
                    "SELECT state FROM memories WHERE memory_id = ?",
                    (predecessor_id,),
                ).fetchone()
                if (
                    predecessor is None
                    or predecessor["state"] != MemoryState.ACCEPTED.value
                ):
                    raise ValueError("la memoire remplacee n'est plus acceptee")
                self._append_transition(
                    connection,
                    predecessor_id,
                    MemoryState.ACCEPTED,
                    MemoryState.SUPERSEDED,
                    actor,
                    f"remplacee par {memory_id}: {reason}",
                    created_at,
                )
                connection.execute(
                    "UPDATE memories SET state = ? WHERE memory_id = ?",
                    (MemoryState.SUPERSEDED.value, predecessor_id),
                )
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        assert row is not None
        return self._memory(row)

    @staticmethod
    def _has_approved_conflict(connection: sqlite3.Connection, memory_id: str) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM contradictions c
                JOIN contradiction_approvals a
                  ON a.contradiction_id = c.contradiction_id
                LEFT JOIN contradiction_resolutions r
                  ON r.contradiction_id = c.contradiction_id
                WHERE r.contradiction_id IS NULL
                  AND (c.left_memory_id = ? OR c.right_memory_id = ?)
                LIMIT 1
                """,
                (memory_id, memory_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _append_transition(
        connection: sqlite3.Connection,
        memory_id: str,
        from_state: MemoryState,
        to_state: MemoryState,
        actor: str,
        reason: str,
        created_at: float,
    ) -> None:
        transition_id = _canonical_hash(
            "tr",
            (
                memory_id,
                from_state.value,
                to_state.value,
                actor,
                reason,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO transitions(
                transition_id, memory_id, from_state, to_state, actor,
                reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                memory_id,
                from_state.value,
                to_state.value,
                actor,
                reason,
                created_at,
            ),
        )

    @staticmethod
    def _assert_acceptable(
        connection: sqlite3.Connection,
        memory_id: str,
        actor: str,
        *,
        at: float | None = None,
    ) -> None:
        evidence = connection.execute(
            """
            SELECT e.kind, e.actor, e.observed_at, e.created_at FROM evidence e
            JOIN memory_evidence me ON me.evidence_id = e.evidence_id
            WHERE me.memory_id = ?
            """,
            (memory_id,),
        ).fetchall()
        if at is not None and any(
            float(row["created_at"]) > at or float(row["observed_at"]) > at
            for row in evidence
        ):
            raise ValueError(
                "une preuve posterieure ne peut pas justifier l'acceptation"
            )
        trusted = [
            row
            for row in evidence
            if EvidenceKind(row["kind"]) in TRUSTED_EVIDENCE
            and not row["actor"].startswith(("model:", "synthetic:"))
        ]
        if not trusted:
            raise ValueError(
                "une proposition de modele ou un test ne suffit pas a accepter un fait"
            )
        if any(row["actor"] == actor for row in evidence):
            raise ValueError(
                "la validation doit etre independante de toutes les preuves"
            )
        validator = connection.execute(
            """
            SELECT actor FROM transitions
            WHERE memory_id = ? AND to_state = ?
              AND (? IS NULL OR created_at <= ?)
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (memory_id, MemoryState.VALIDATED.value, at, at),
        ).fetchone()
        if validator is None or validator["actor"] == actor:
            raise ValueError("validation et acceptation exigent deux acteurs distincts")
        unresolved = connection.execute(
            """
            SELECT 1 FROM contradictions c
            JOIN contradiction_approvals a
              ON a.contradiction_id = c.contradiction_id
            WHERE (c.left_memory_id = ? OR c.right_memory_id = ?)
              AND (? IS NULL OR (c.created_at <= ? AND a.created_at <= ?))
              AND NOT EXISTS (
                  SELECT 1 FROM contradiction_resolutions historical_resolution
                  WHERE historical_resolution.contradiction_id = c.contradiction_id
                    AND (? IS NULL OR historical_resolution.created_at <= ?)
              )
            LIMIT 1
            """,
            (memory_id, memory_id, at, at, at, at, at),
        ).fetchone()
        if unresolved is not None:
            raise ValueError("une contradiction non resolue interdit l'acceptation")

    def contradict(
        self,
        left_memory_id: str,
        right_memory_id: str,
        *,
        authority: VerifiedPrincipal,
        reason: str,
    ) -> str:
        if left_memory_id == right_memory_id:
            raise ValueError("une memoire ne peut pas se contredire elle-meme")
        authority = self._require_authority(
            authority, AuthorityCapability.PROPOSE_CONTRADICTION
        )
        actor = authority.actor
        reason = _clean(reason, "reason", maximum=1_000)
        if _SECRET.search(actor) or _SECRET.search(reason):
            raise ValueError("un secret potentiel ne peut pas entrer dans le journal")
        left, right = sorted((left_memory_id, right_memory_id))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT memory_id, semantic_key, claim_key, kind, subject_scope, state "
                "FROM memories "
                "WHERE memory_id IN (?, ?)",
                (left, right),
            ).fetchall()
            if len(rows) != 2:
                raise ValueError("memoire inconnue dans la contradiction")
            for row in rows:
                self._require_write_scope(authority, str(row["subject_scope"]))
            comparable = {
                (row["kind"], row["subject_scope"], row["claim_key"]) for row in rows
            }
            if len(comparable) != 1:
                raise ValueError(
                    "les souvenirs contradictoires exigent la meme portee, "
                    "le meme type et la meme claim_key"
                )
            if len({row["semantic_key"] for row in rows}) != 2:
                raise ValueError(
                    "deux versions du meme fait ne sont pas contradictoires"
                )
            if any(
                row["state"]
                in {MemoryState.SUPERSEDED.value, MemoryState.REVOKED.value}
                for row in rows
            ):
                raise ValueError("une version terminale ne peut pas rouvrir un conflit")
            active = connection.execute(
                """
                SELECT c.contradiction_id FROM contradictions c
                LEFT JOIN contradiction_resolutions r
                  ON r.contradiction_id = c.contradiction_id
                WHERE c.left_memory_id = ? AND c.right_memory_id = ?
                  AND r.contradiction_id IS NULL
                LIMIT 1
                """,
                (left, right),
            ).fetchone()
            if active is not None:
                return str(active["contradiction_id"])
            created_at = time.time()
            contradiction_id = _canonical_hash(
                "conflict", (left, right, actor, reason, created_at)
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO contradictions(
                    contradiction_id, left_memory_id, right_memory_id, actor,
                    reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (contradiction_id, left, right, actor, reason, created_at),
            )
        return contradiction_id

    def approve_contradiction(
        self,
        contradiction_id: str,
        *,
        authority: VerifiedPrincipal,
        reason: str,
    ) -> None:
        authority = self._require_authority(
            authority, AuthorityCapability.APPROVE_CONTRADICTION
        )
        actor = authority.actor
        _acceptance_actor(actor)
        reason = _clean(reason, "reason", maximum=1_000)
        if _SECRET.search(actor) or _SECRET.search(reason):
            raise ValueError("un secret potentiel ne peut pas entrer dans le journal")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT c.*, a.contradiction_id AS approved,
                       r.contradiction_id AS resolved
                FROM contradictions c
                LEFT JOIN contradiction_approvals a
                  ON a.contradiction_id = c.contradiction_id
                LEFT JOIN contradiction_resolutions r
                  ON r.contradiction_id = c.contradiction_id
                WHERE c.contradiction_id = ?
                """,
                (contradiction_id,),
            ).fetchone()
            if row is None or row["resolved"]:
                raise ValueError("contradiction inconnue ou deja resolue")
            if row["approved"]:
                raise ValueError("contradiction deja approuvee")
            if row["actor"] == actor:
                raise ValueError(
                    "proposition et approbation exigent deux acteurs distincts"
                )
            states = connection.execute(
                "SELECT state FROM memories WHERE memory_id IN (?, ?)",
                (row["left_memory_id"], row["right_memory_id"]),
            ).fetchall()
            if any(
                item["state"]
                in {MemoryState.SUPERSEDED.value, MemoryState.REVOKED.value}
                for item in states
            ):
                raise ValueError("une version terminale ne peut pas entrer en conflit")
            approved_at = time.time()
            record_hash = _canonical_hash(
                "approval", (contradiction_id, actor, reason, approved_at)
            )
            connection.execute(
                """
                INSERT INTO contradiction_approvals(
                    contradiction_id, actor, reason, created_at, record_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (contradiction_id, actor, reason, approved_at, record_hash),
            )

    def resolve_contradiction(
        self,
        contradiction_id: str,
        *,
        winner_memory_id: str,
        authority: VerifiedPrincipal,
        reason: str,
    ) -> None:
        authority = self._require_authority(
            authority, AuthorityCapability.RESOLVE_CONTRADICTION
        )
        actor = authority.actor
        reason = _clean(reason, "reason", maximum=1_000)
        if _SECRET.search(actor) or _SECRET.search(reason):
            raise ValueError("un secret potentiel ne peut pas entrer dans le journal")
        _acceptance_actor(actor)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT c.*, a.contradiction_id AS approved,
                       r.contradiction_id AS resolved
                FROM contradictions c
                LEFT JOIN contradiction_approvals a
                  ON a.contradiction_id = c.contradiction_id
                LEFT JOIN contradiction_resolutions r
                  ON r.contradiction_id = c.contradiction_id
                WHERE c.contradiction_id = ?
                """,
                (contradiction_id,),
            ).fetchone()
            if row is None or row["resolved"]:
                raise ValueError("contradiction inconnue ou deja resolue")
            if not row["approved"]:
                raise ValueError(
                    "la contradiction doit etre approuvee avant resolution"
                )
            if winner_memory_id not in {row["left_memory_id"], row["right_memory_id"]}:
                raise ValueError("le gagnant doit appartenir a la contradiction")
            loser_memory_id = (
                row["right_memory_id"]
                if winner_memory_id == row["left_memory_id"]
                else row["left_memory_id"]
            )
            states = {
                state_row["memory_id"]: MemoryState(state_row["state"])
                for state_row in connection.execute(
                    "SELECT memory_id, state FROM memories WHERE memory_id IN (?, ?)",
                    (winner_memory_id, loser_memory_id),
                )
            }
            if states[winner_memory_id] in {
                MemoryState.SUPERSEDED,
                MemoryState.REVOKED,
            }:
                raise ValueError("une version terminale ne peut pas gagner")
            loser_state = states[loser_memory_id]
            if loser_state in {MemoryState.SUPERSEDED, MemoryState.REVOKED}:
                raise ValueError("la version perdante est deja terminale")
            resolved_at = time.time()
            record_hash = _canonical_hash(
                "resolution",
                (contradiction_id, winner_memory_id, actor, reason, resolved_at),
            )
            connection.execute(
                """
                INSERT INTO contradiction_resolutions(
                    contradiction_id, winner_memory_id, actor, reason, created_at,
                    record_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    contradiction_id,
                    winner_memory_id,
                    actor,
                    reason,
                    resolved_at,
                    record_hash,
                ),
            )
            self._append_transition(
                connection,
                loser_memory_id,
                loser_state,
                MemoryState.REVOKED,
                actor,
                f"contradiction {contradiction_id}: {reason}",
                resolved_at,
            )
            connection.execute(
                "UPDATE memories SET state = ? WHERE memory_id = ?",
                (MemoryState.REVOKED.value, loser_memory_id),
            )

    def memories_for(
        self,
        principal: VerifiedPrincipal,
        *,
        now: float | None = None,
    ) -> list[Memory]:
        principal = self._require_issued_authority(principal)
        requester_scope = principal.subject_scope
        now = float(now if now is not None else time.time())
        if not math.isfinite(now):
            raise ValueError("now doit etre fini")
        audience: list[tuple[str, Visibility]]
        audience = []
        if (
            requester_scope.startswith("person:")
            and AuthorityCapability.READ_PRIVATE in principal.capabilities
        ):
            audience = [(requester_scope, Visibility.PRIVATE)]
        elif requester_scope.startswith("task:"):
            if AuthorityCapability.READ_PRIVATE in principal.capabilities:
                audience.append((requester_scope, Visibility.PRIVATE))
            if AuthorityCapability.READ_INTERNAL in principal.capabilities:
                audience.append((requester_scope, Visibility.INTERNAL))
        if (
            requester_scope.startswith("household:")
            and AuthorityCapability.READ_HOUSEHOLD in principal.capabilities
        ):
            audience.append((requester_scope, Visibility.HOUSEHOLD))
        if (
            principal.household_scope
            and AuthorityCapability.READ_HOUSEHOLD in principal.capabilities
        ):
            audience.append((principal.household_scope, Visibility.HOUSEHOLD))
        if (
            requester_scope.startswith("system:")
            and AuthorityCapability.READ_INTERNAL in principal.capabilities
        ):
            audience.append((requester_scope, Visibility.INTERNAL))
        if AuthorityCapability.READ_INTERNAL in principal.capabilities:
            audience.extend(
                (scope, Visibility.INTERNAL) for scope in principal.system_scopes
            )
        audience = list(dict.fromkeys(audience))
        if not audience:
            return []
        clauses = " OR ".join(
            "(m.subject_scope = ? AND m.visibility = ?)" for _ in audience
        )
        audience_parameters = [
            value
            for scope, visibility in audience
            for value in (scope, visibility.value)
        ]
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT m.* FROM memories m
                WHERE m.state = ? AND ({clauses})
                  AND m.valid_from <= ? AND (m.valid_until = 0 OR m.valid_until > ?)
                  AND (m.sensitivity != ? OR ? = 1)
                  AND NOT EXISTS (
                      SELECT 1 FROM contradictions c
                      JOIN contradiction_approvals a
                        ON a.contradiction_id = c.contradiction_id
                      LEFT JOIN contradiction_resolutions r
                        ON r.contradiction_id = c.contradiction_id
                      WHERE r.contradiction_id IS NULL
                        AND (c.left_memory_id = m.memory_id
                             OR c.right_memory_id = m.memory_id)
                  )
                ORDER BY m.created_at DESC
                """,
                (
                    MemoryState.ACCEPTED.value,
                    *audience_parameters,
                    now,
                    now,
                    Sensitivity.RESTRICTED.value,
                    int(AuthorityCapability.READ_RESTRICTED in principal.capabilities),
                ),
            ).fetchall()
        return [self._memory(row) for row in rows]

    def obsidian_projection(
        self,
        authority: VerifiedPrincipal,
        *,
        now: float | None = None,
    ) -> list[Memory]:
        authority = self._require_authority(
            authority, AuthorityCapability.READ_INTERNAL
        )
        now = float(now if now is not None else time.time())
        if not math.isfinite(now):
            raise ValueError("now doit etre fini")
        return [
            memory
            for memory in self.memories_for(authority, now=now)
            if memory.projection is Projection.OBSIDIAN
            and memory.visibility is Visibility.INTERNAL
            and memory.sensitivity is Sensitivity.INTERNAL
        ]

    def transitions_for(
        self, memory_id: str, *, authority: VerifiedPrincipal
    ) -> list[sqlite3.Row]:
        self._require_authority(authority, AuthorityCapability.READ_AUDIT)
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM transitions WHERE memory_id = ? ORDER BY created_at",
                    (memory_id,),
                ).fetchall()
            )

    def integrity_check(self) -> None:
        with self._connect() as connection:
            self._assert_database_integrity(connection)

    @classmethod
    def _assert_database_integrity(cls, connection: sqlite3.Connection) -> None:
        cls._assert_schema_shape(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise RuntimeError("integrite SQLite invalide")
        errors = cls._logical_integrity_errors(connection)
        if errors:
            raise RuntimeError("integrite logique invalide: " + "; ".join(errors[:5]))

    @classmethod
    def _logical_integrity_errors(cls, connection: sqlite3.Connection) -> list[str]:
        errors: list[str] = []
        meta = connection.execute(
            "SELECT singleton, version FROM schema_meta"
        ).fetchall()
        if len(meta) != 1 or meta[0]["singleton"] != 1 or meta[0]["version"] != 2:
            errors.append("schema_meta incoherent")

        evidence_rows = {
            row["evidence_id"]: row
            for row in connection.execute("SELECT * FROM evidence")
        }
        for evidence_id, row in evidence_rows.items():
            try:
                kind = EvidenceKind(row["kind"])
                _validate_evidence_actor(kind, row["actor"])
                _scope(row["subject_scope"])
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", row["content_hash"]):
                    raise ValueError("hash invalide")
                if any(
                    _SECRET.search(value)
                    for value in (
                        row["source_ref"],
                        row["actor"],
                        row["trace_id"],
                        row["correlation_id"],
                    )
                    if value
                ):
                    raise ValueError("secret potentiel")
                if not all(
                    math.isfinite(float(value))
                    for value in (row["observed_at"], row["created_at"])
                ):
                    raise ValueError("temps non fini")
                expected_id = _canonical_hash(
                    "ev",
                    (
                        kind.value,
                        row["source_ref"],
                        row["actor"],
                        row["subject_scope"],
                        row["content_hash"],
                        row["trace_id"],
                        row["correlation_id"],
                    ),
                )
                if evidence_id != expected_id:
                    raise ValueError("identifiant non canonique")
                expected_record_hash = _canonical_hash(
                    "evrecord",
                    (
                        evidence_id,
                        float(row["observed_at"]),
                        float(row["created_at"]),
                    ),
                )
                if row["record_hash"] != expected_record_hash:
                    raise ValueError("empreinte d'enregistrement non canonique")
            except (TypeError, ValueError) as exc:
                errors.append(f"preuve {evidence_id}: {exc}")

        memory_rows = {
            row["memory_id"]: row
            for row in connection.execute("SELECT * FROM memories")
        }
        evidence_links: dict[str, list[str]] = {
            memory_id: [] for memory_id in memory_rows
        }
        for link in connection.execute("SELECT * FROM memory_evidence"):
            if link["memory_id"] in evidence_links:
                evidence_links[link["memory_id"]].append(link["evidence_id"])

        for memory_id, row in memory_rows.items():
            try:
                kind = MemoryKind(row["kind"])
                sensitivity = Sensitivity(row["sensitivity"])
                visibility = Visibility(row["visibility"])
                projection = Projection(row["projection"])
                MemoryState(row["state"])
                statement = _statement(row["statement"])
                subject_scope = _scope(row["subject_scope"])
                claim_key = _claim_key(row["claim_key"])
                proposed_by = _clean(row["proposed_by"], "proposed_by")
                if _SECRET.search(proposed_by):
                    raise ValueError("proposed_by contient un secret potentiel")
                _validate_memory_policy(
                    kind, subject_scope, sensitivity, visibility, projection
                )
                confidence = float(row["confidence"])
                valid_from = float(row["valid_from"])
                valid_until = float(row["valid_until"])
                created_at = float(row["created_at"])
                if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError("confidence invalide")
                if not all(
                    math.isfinite(value)
                    for value in (valid_from, valid_until, created_at)
                ):
                    raise ValueError("temps non fini")
                if valid_until and valid_until <= valid_from:
                    raise ValueError("fenetre temporelle invalide")
                evidence_ids = sorted(evidence_links[memory_id])
                if not evidence_ids:
                    raise ValueError("aucune preuve associee")
                if any(
                    evidence_rows[evidence_id]["subject_scope"] != subject_scope
                    for evidence_id in evidence_ids
                    if evidence_id in evidence_rows
                ):
                    raise ValueError("portee de preuve incoherente")
                if any(
                    float(evidence_rows[evidence_id]["created_at"]) > created_at
                    for evidence_id in evidence_ids
                    if evidence_id in evidence_rows
                ):
                    raise ValueError("preuve creee apres la memoire")
                semantic_key = _canonical_hash(
                    "concept",
                    (
                        kind.value,
                        subject_scope,
                        " ".join(statement.casefold().split()),
                    ),
                )
                if row["semantic_key"] != semantic_key:
                    raise ValueError("semantic_key non canonique")
                expected_id = _canonical_hash(
                    "memv",
                    (
                        semantic_key,
                        claim_key,
                        evidence_ids,
                        sensitivity.value,
                        visibility.value,
                        projection.value,
                        confidence,
                        valid_from,
                        valid_until,
                        row["supersedes_id"],
                    ),
                )
                if memory_id != expected_id:
                    raise ValueError("identifiant non canonique")
                expected_record_hash = _canonical_hash(
                    "memrecord", (memory_id, created_at, proposed_by)
                )
                if row["record_hash"] != expected_record_hash:
                    raise ValueError("empreinte d'enregistrement non canonique")
                if row["supersedes_id"]:
                    predecessor = memory_rows.get(row["supersedes_id"])
                    if predecessor is None:
                        raise ValueError("predecesseur absent")
                    if (
                        predecessor["subject_scope"],
                        predecessor["kind"],
                        predecessor["claim_key"],
                    ) != (subject_scope, kind.value, claim_key):
                        raise ValueError("predecesseur non comparable")
                    if (
                        row["state"] == MemoryState.ACCEPTED.value
                        and predecessor["state"] != MemoryState.SUPERSEDED.value
                    ):
                        raise ValueError("supersession non atomique")
                if row["state"] == MemoryState.SUPERSEDED.value:
                    successors = connection.execute(
                        """
                        SELECT COUNT(DISTINCT successor.memory_id)
                        FROM memories successor
                        JOIN transitions t ON t.memory_id = successor.memory_id
                        WHERE successor.supersedes_id = ? AND t.to_state = ?
                        """,
                        (memory_id, MemoryState.ACCEPTED.value),
                    ).fetchone()[0]
                    if successors != 1:
                        raise ValueError("supersession sans successeur accepte unique")
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"memoire {memory_id}: {exc}")

        allowed = {
            MemoryState.CANDIDATE: {MemoryState.VALIDATED, MemoryState.REVOKED},
            MemoryState.VALIDATED: {MemoryState.ACCEPTED, MemoryState.REVOKED},
            MemoryState.ACCEPTED: {MemoryState.SUPERSEDED, MemoryState.REVOKED},
            MemoryState.SUPERSEDED: set(),
            MemoryState.REVOKED: set(),
        }
        for memory_id, memory in memory_rows.items():
            current = MemoryState.CANDIDATE
            transitions = connection.execute(
                "SELECT rowid, * FROM transitions WHERE memory_id = ? "
                "ORDER BY created_at, rowid",
                (memory_id,),
            ).fetchall()
            for transition in transitions:
                try:
                    from_state = MemoryState(transition["from_state"])
                    to_state = MemoryState(transition["to_state"])
                    actor = _clean(transition["actor"], "actor")
                    reason = _clean(transition["reason"], "reason", maximum=1_000)
                    created_at = float(transition["created_at"])
                    if _SECRET.search(actor) or _SECRET.search(reason):
                        raise ValueError("secret potentiel")
                    if not math.isfinite(created_at):
                        raise ValueError("date non finie")
                    if created_at < float(memory["created_at"]):
                        raise ValueError("transition anterieure a la memoire")
                    if to_state in {
                        MemoryState.ACCEPTED,
                        MemoryState.SUPERSEDED,
                    }:
                        _acceptance_actor(actor)
                    if to_state is MemoryState.VALIDATED and actor == str(
                        memory["proposed_by"]
                    ):
                        raise ValueError("validation non independante de l'auteur")
                    if to_state is MemoryState.ACCEPTED:
                        cls._assert_acceptable(
                            connection,
                            memory_id,
                            actor,
                            at=created_at,
                        )
                    if from_state is not current or to_state not in allowed[current]:
                        raise ValueError("chaine d'etats invalide")
                    expected_id = _canonical_hash(
                        "tr",
                        (
                            memory_id,
                            from_state.value,
                            to_state.value,
                            actor,
                            reason,
                            created_at,
                        ),
                    )
                    if transition["transition_id"] != expected_id:
                        raise ValueError("identifiant non canonique")
                    current = to_state
                except (TypeError, ValueError) as exc:
                    errors.append(f"transition {transition['transition_id']}: {exc}")
                    break
            try:
                persisted_state = MemoryState(memory["state"])
            except ValueError:
                continue
            if current is not persisted_state:
                errors.append(f"memoire {memory_id}: etat et journal divergent")

        contradictions = {
            row["contradiction_id"]: row
            for row in connection.execute("SELECT * FROM contradictions")
        }
        approvals = {
            row["contradiction_id"]: row
            for row in connection.execute("SELECT * FROM contradiction_approvals")
        }
        resolutions = {
            row["contradiction_id"]: row
            for row in connection.execute("SELECT * FROM contradiction_resolutions")
        }
        terminal = {MemoryState.SUPERSEDED.value, MemoryState.REVOKED.value}
        for contradiction_id, contradiction in contradictions.items():
            try:
                left_id = contradiction["left_memory_id"]
                right_id = contradiction["right_memory_id"]
                left = memory_rows[left_id]
                right = memory_rows[right_id]
                if left_id >= right_id:
                    raise ValueError("ordre des membres non canonique")
                if (
                    left["kind"],
                    left["subject_scope"],
                    left["claim_key"],
                ) != (
                    right["kind"],
                    right["subject_scope"],
                    right["claim_key"],
                ):
                    raise ValueError("membres non comparables")
                if left["semantic_key"] == right["semantic_key"]:
                    raise ValueError("versions semantiquement identiques")
                proposal_actor = _clean(contradiction["actor"], "actor")
                proposal_reason = _clean(
                    contradiction["reason"], "reason", maximum=1_000
                )
                proposal_time = float(contradiction["created_at"])
                if _SECRET.search(proposal_actor) or _SECRET.search(proposal_reason):
                    raise ValueError("secret potentiel dans la proposition")
                if not math.isfinite(proposal_time):
                    raise ValueError("date non finie")
                expected_contradiction_id = _canonical_hash(
                    "conflict",
                    (
                        left_id,
                        right_id,
                        proposal_actor,
                        proposal_reason,
                        proposal_time,
                    ),
                )
                if contradiction_id != expected_contradiction_id:
                    raise ValueError("identifiant de contradiction non canonique")
                approval = approvals.get(contradiction_id)
                resolution = resolutions.get(contradiction_id)
                if approval is not None:
                    approval_actor = _clean(approval["actor"], "actor")
                    approval_reason = _clean(
                        approval["reason"], "reason", maximum=1_000
                    )
                    approval_time = float(approval["created_at"])
                    _acceptance_actor(approval_actor)
                    if _SECRET.search(approval_actor) or _SECRET.search(
                        approval_reason
                    ):
                        raise ValueError("secret potentiel dans l'approbation")
                    if approval_actor == proposal_actor:
                        raise ValueError("approbation non independante")
                    if not math.isfinite(approval_time):
                        raise ValueError("date d'approbation non finie")
                    if approval_time < proposal_time:
                        raise ValueError("approbation anterieure a la proposition")
                    expected_approval_hash = _canonical_hash(
                        "approval",
                        (
                            contradiction_id,
                            approval_actor,
                            approval_reason,
                            approval_time,
                        ),
                    )
                    if approval["record_hash"] != expected_approval_hash:
                        raise ValueError("empreinte d'approbation non canonique")
                if resolution is not None:
                    if approval is None:
                        raise ValueError("resolution sans approbation")
                    if resolution["winner_memory_id"] not in {left_id, right_id}:
                        raise ValueError("gagnant exterieur au conflit")
                    loser_id = (
                        right_id
                        if resolution["winner_memory_id"] == left_id
                        else left_id
                    )
                    if memory_rows[loser_id]["state"] != MemoryState.REVOKED.value:
                        raise ValueError("perdant non revoque")
                    resolution_actor = _clean(resolution["actor"], "actor")
                    resolution_reason = _clean(
                        resolution["reason"], "reason", maximum=1_000
                    )
                    resolution_time = float(resolution["created_at"])
                    _acceptance_actor(resolution_actor)
                    if _SECRET.search(resolution_actor) or _SECRET.search(
                        resolution_reason
                    ):
                        raise ValueError("secret potentiel dans la resolution")
                    if not math.isfinite(resolution_time):
                        raise ValueError("date de resolution non finie")
                    if resolution_time < approval_time:
                        raise ValueError("resolution anterieure a l'approbation")
                    expected_resolution_hash = _canonical_hash(
                        "resolution",
                        (
                            contradiction_id,
                            resolution["winner_memory_id"],
                            resolution_actor,
                            resolution_reason,
                            resolution_time,
                        ),
                    )
                    if resolution["record_hash"] != expected_resolution_hash:
                        raise ValueError("empreinte de resolution non canonique")
                    revocation = connection.execute(
                        """
                        SELECT 1 FROM transitions
                        WHERE memory_id = ? AND to_state = ? AND actor = ?
                          AND reason = ? AND created_at = ?
                        """,
                        (
                            loser_id,
                            MemoryState.REVOKED.value,
                            resolution_actor,
                            f"contradiction {contradiction_id}: {resolution_reason}",
                            resolution_time,
                        ),
                    ).fetchone()
                    if revocation is None:
                        raise ValueError("resolution sans transition de revocation")
                elif approval is not None and (
                    left["state"] in terminal or right["state"] in terminal
                ):
                    raise ValueError("conflit approuve avec une version terminale")
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"contradiction {contradiction_id}: {exc}")
        return errors

    def backup(self, destination: str | Path, *, replace: bool = False) -> Path:
        target = Path(destination).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
        if target.is_symlink():
            raise ValueError("la destination de sauvegarde ne peut pas etre un lien")
        if target.resolve() == self.path.resolve():
            raise ValueError("la sauvegarde doit avoir un chemin distinct")
        if target.exists() and not replace:
            raise FileExistsError(target)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary_sidecars = (
            Path(f"{temporary}-wal"),
            Path(f"{temporary}-shm"),
        )
        try:
            os.chmod(temporary, 0o600)
            with self._connect() as source, sqlite3.connect(temporary) as output:
                output.row_factory = sqlite3.Row
                output.execute("PRAGMA foreign_keys = ON")
                source.backup(output)
                self._assert_database_integrity(output)
                if output.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                    checkpoint = output.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if checkpoint is None or tuple(checkpoint) != (0, 0, 0):
                        raise RuntimeError("checkpoint de sauvegarde SQLite incomplet")
                    journal_mode = output.execute(
                        "PRAGMA journal_mode = DELETE"
                    ).fetchone()[0]
                    if journal_mode != "delete":
                        raise RuntimeError("journal WAL de sauvegarde non neutralise")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            for sidecar in temporary_sidecars:
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
        return target

    @staticmethod
    def _memory(row: sqlite3.Row) -> Memory:
        return Memory(
            memory_id=row["memory_id"],
            semantic_key=row["semantic_key"],
            claim_key=row["claim_key"],
            kind=MemoryKind(row["kind"]),
            statement=row["statement"],
            subject_scope=row["subject_scope"],
            sensitivity=Sensitivity(row["sensitivity"]),
            visibility=Visibility(row["visibility"]),
            projection=Projection(row["projection"]),
            confidence=float(row["confidence"]),
            state=MemoryState(row["state"]),
            created_at=float(row["created_at"]),
            valid_from=float(row["valid_from"]),
            valid_until=float(row["valid_until"]),
            supersedes_id=row["supersedes_id"],
            proposed_by=row["proposed_by"],
        )


__all__ = [
    "AuthorityCapability",
    "AuthorityClaims",
    "Evidence",
    "EvidenceKind",
    "GovernedMemoryStore",
    "Memory",
    "MemoryKind",
    "MemoryState",
    "Projection",
    "SCHEMA_VERSION",
    "Sensitivity",
    "Visibility",
    "VerifiedPrincipal",
]
