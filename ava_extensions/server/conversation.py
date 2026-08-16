"""Server-side conversation history, partitioned by a verified principal.

OIDC signatures and claims (or a bounded CP service assertion) are verified in
``ava_extensions.server.principal`` before a storage key is returned.  Invalid,
expired, anonymous, or ambiguous requests never share a fallback bucket.  The
historical ``sub:<subject>`` OIDC key is preserved so strengthening the trust
boundary does not orphan existing private conversations.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ava_extensions.server.principal import resolve_request_principal
from openjarvis.server.models import MAX_COMPLETION_TOKENS, ChatCompletionResponse

logger = logging.getLogger(__name__)

CHEMIN_BASE = Path.home() / ".openjarvis" / "ava-conversations.db"
TURN_ID_HEADER = "X-Ava-Turn-Id"

# ⚠ Plafond par utilisateur. Une conversation n'est pas une archive : au-delà, le
#   coût de lecture croît sans que les tours anciens servent encore. 2000 lignes
#   couvrent plusieurs mois d'usage quotidien ; les plus anciennes sortent.
MAX_LIGNES = 2000

# A pending key may represent an agent/tool effect whose outcome is ambiguous after a
# crash. It is therefore never aged out automatically. Bound new reservations instead:
# explicit reconciliation/deletion remains the only safe way to release stale keys.
MAX_TOURS_PENDING_PAR_UTILISATEUR = 32

# Idempotency keys are never silently forgotten: doing so would let a very late
# network retry execute model/tool effects again.  The trade-off is a hard,
# fail-closed cardinality ceiling per verified principal.  At the default this
# represents decades of ordinary daily use while keeping an abusive stream of
# fresh UUIDs from growing SQLite without bound.  Replays of already-known keys
# remain available even after the ceiling is reached.
MAX_TURN_KEYS_PAR_UTILISATEUR = 100_000

# ⚠ Un verrou : SQLite tolère les accès concurrents mais pas deux écritures simultanées
#   sur la même connexion. Le daemon est asynchrone — deux onglets ouverts suffisent.
_verrou = threading.Lock()


class ConversationStorageError(RuntimeError):
    """The durable conversation store could not complete an operation."""


class TurnCollisionError(RuntimeError):
    """A turn identifier already names different immutable content."""


class PendingTurnLimitError(RuntimeError):
    """A principal must reconcile pending turns before reserving another one."""


class TurnKeyLimitError(RuntimeError):
    """A principal's immutable idempotency journal reached its hard ceiling."""


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One immutable, principal-scoped user/assistant exchange."""

    turn_id: str
    user_text: str
    assistant_text: str
    timestamp: float


@dataclass(frozen=True, slots=True)
class TurnWriteResult:
    """Outcome of an idempotent turn write."""

    turn: ConversationTurn
    created: bool


@dataclass(frozen=True, slots=True)
class TurnJournalEntry:
    """Pre-generation idempotency journal entry."""

    turn_id: str
    user_text: str
    assistant_text: str | None
    timestamp: float
    state: Literal["pending", "completed", "abandoned"]
    abandon_reason: str | None = None
    request_sha256: str | None = None
    response_json: str | None = None


@dataclass(frozen=True, slots=True)
class TurnReservationResult:
    """Outcome of reserving an idempotency key before model/tool execution."""

    entry: TurnJournalEntry
    created: bool


@dataclass(frozen=True, slots=True)
class TurnReconciliationResult:
    """Audited, principal-scoped abandonment of one ambiguous pending turn."""

    turn_id: str
    abandoned: bool


def normaliser_turn_id(turn_id: str | uuid.UUID) -> str:
    """Return the canonical UUID spelling used by the unique index."""

    try:
        return str(uuid.UUID(str(turn_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("turn_id must be a UUID") from exc


def _refuser_ancetre_symbolique(parent: Path) -> None:
    """Reject a storage directory reached through a symbolic-link ancestor."""

    candidat = parent
    while True:
        try:
            os.lstat(candidat)
            break
        except FileNotFoundError:
            if candidat == candidat.parent:
                raise OSError("conversation storage parent is unavailable")
            candidat = candidat.parent
    if stat.S_ISLNK(os.lstat(candidat).st_mode):
        raise OSError("conversation storage path must not be symbolic")
    if candidat.resolve() != candidat.absolute():
        raise OSError("conversation storage path must not cross a symbolic link")


def _ouvrir_fichier_prive(chemin: Path, *, creer: bool) -> None:
    """Validate a regular file without following its final symlink and chmod it."""

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if creer:
        flags |= os.O_CREAT
    fd = os.open(chemin, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("conversation storage file must be regular")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _securiser_sidecars_sqlite() -> None:
    """Reject symbolic SQLite journals and constrain existing sidecar modes."""

    for suffixe in ("-wal", "-shm", "-journal"):
        chemin = Path(f"{CHEMIN_BASE}{suffixe}")
        try:
            _ouvrir_fichier_prive(chemin, creer=False)
        except FileNotFoundError:
            continue


def _preparer_stockage_prive() -> None:
    """Create the conversation store below a private, non-symbolic directory."""

    parent = CHEMIN_BASE.parent
    _refuser_ancetre_symbolique(parent)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_stat = os.lstat(parent)
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise OSError("conversation storage parent must be a real directory")
    if parent.resolve() != parent.absolute():
        raise OSError("conversation storage parent must not cross a symbolic link")
    os.chmod(parent, 0o700, follow_symlinks=False)

    try:
        existing = os.lstat(CHEMIN_BASE)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise OSError("conversation storage file must be regular and non-symbolic")
    _ouvrir_fichier_prive(CHEMIN_BASE, creer=True)
    _securiser_sidecars_sqlite()


def _colonnes_lignes(cx: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in cx.execute("PRAGMA table_info(lignes)")}


def _colonnes_reconciliations(cx: sqlite3.Connection) -> set[str]:
    return {
        str(row[1]) for row in cx.execute("PRAGMA table_info(turn_reconciliations)")
    }


def _colonnes_tours(cx: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in cx.execute("PRAGMA table_info(tours)")}


def _enregistrer_tombstone(
    cx: sqlite3.Connection,
    utilisateur: str,
    turn_id: str,
    *,
    reason: str,
) -> None:
    """Retain only an idempotency key after conversation content is erased."""

    cx.execute(
        "INSERT OR IGNORE INTO turn_tombstones "
        "(utilisateur, turn_id, reason, horodatage) VALUES (?,?,?,?)",
        (utilisateur, turn_id, reason, time.time()),
    )


def _tombstone_existe(
    cx: sqlite3.Connection,
    utilisateur: str,
    turn_id: str,
) -> bool:
    return (
        cx.execute(
            "SELECT 1 FROM turn_tombstones WHERE utilisateur = ? AND turn_id = ?",
            (utilisateur, turn_id),
        ).fetchone()
        is not None
    )


def _verifier_capacite_cle_tour(
    cx: sqlite3.Connection,
    utilisateur: str,
) -> None:
    """Refuse a fresh UUID before the immutable key journal can grow further."""

    nombre = int(
        cx.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT turn_id FROM tours WHERE utilisateur = ? "
            "UNION SELECT turn_id FROM turn_tombstones WHERE utilisateur = ? "
            "UNION SELECT turn_id FROM turn_reconciliations WHERE utilisateur = ?"
            ")",
            (utilisateur, utilisateur, utilisateur),
        ).fetchone()[0]
    )
    if nombre >= MAX_TURN_KEYS_PAR_UTILISATEUR:
        raise TurnKeyLimitError("conversation idempotency journal is full")


def _migrer_schema(cx: sqlite3.Connection) -> None:
    """Add turn metadata without rewriting or orphaning historical rows."""

    colonnes = _colonnes_lignes(cx)
    if "turn_id" not in colonnes:
        cx.execute("ALTER TABLE lignes ADD COLUMN turn_id TEXT")
    if "turn_position" not in colonnes:
        cx.execute("ALTER TABLE lignes ADD COLUMN turn_position INTEGER")
    cx.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_util_turn_position "
        "ON lignes(utilisateur, turn_id, turn_position) "
        "WHERE turn_id IS NOT NULL"
    )
    cx.execute(
        """
        CREATE TABLE IF NOT EXISTS tours (
            utilisateur    TEXT NOT NULL,
            turn_id        TEXT NOT NULL,
            user_text      TEXT NOT NULL,
            assistant_text TEXT,
            request_sha256 TEXT,
            response_json   TEXT,
            horodatage     REAL NOT NULL,
            state          TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
            PRIMARY KEY (utilisateur, turn_id)
        )
        """
    )
    if "request_sha256" not in _colonnes_tours(cx):
        cx.execute("ALTER TABLE tours ADD COLUMN request_sha256 TEXT")
    if "response_json" not in _colonnes_tours(cx):
        cx.execute("ALTER TABLE tours ADD COLUMN response_json TEXT")
    cx.execute(
        "CREATE INDEX IF NOT EXISTS idx_tours_pending_utilisateur "
        "ON tours(utilisateur) WHERE state = 'pending'"
    )
    cx.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_tombstones (
            utilisateur TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN ('retention', 'deleted')),
            horodatage REAL NOT NULL,
            PRIMARY KEY (utilisateur, turn_id)
        )
        """
    )
    cx.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_reconciliations (
            utilisateur TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action = 'abandoned'),
            user_text_sha256 TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'manual_reconciliation',
            assistant_text_sha256 TEXT,
            horodatage REAL NOT NULL,
            PRIMARY KEY (utilisateur, turn_id)
        )
        """
    )
    reconciliation_columns = _colonnes_reconciliations(cx)
    if "reason" not in reconciliation_columns:
        cx.execute(
            "ALTER TABLE turn_reconciliations ADD COLUMN reason TEXT "
            "NOT NULL DEFAULT 'manual_reconciliation'"
        )
    if "assistant_text_sha256" not in reconciliation_columns:
        cx.execute(
            "ALTER TABLE turn_reconciliations ADD COLUMN assistant_text_sha256 TEXT"
        )
    # Backfill an early version of the additive migration which annotated only
    # ``lignes``. Historical rows with NULL turn metadata remain untouched.
    cx.execute(
        """
        INSERT OR IGNORE INTO tours (
            utilisateur, turn_id, user_text, assistant_text, horodatage, state
        )
        SELECT
            utilisateur,
            turn_id,
            MAX(CASE WHEN turn_position = 0 AND role = 'user' THEN texte END),
            MAX(CASE WHEN turn_position = 1 AND role = 'assistant' THEN texte END),
            MIN(horodatage),
            'completed'
        FROM lignes
        WHERE turn_id IS NOT NULL
        GROUP BY utilisateur, turn_id
        HAVING COUNT(*) = 2
           AND COUNT(CASE WHEN turn_position = 0 AND role = 'user' THEN 1 END) = 1
           AND COUNT(CASE WHEN turn_position = 1 AND role = 'assistant' THEN 1 END) = 1
        """
    )
    cx.commit()


def _connexion() -> sqlite3.Connection:
    _preparer_stockage_prive()
    cx: sqlite3.Connection | None = None
    try:
        cx = sqlite3.connect(CHEMIN_BASE, timeout=10)
        cx.execute("PRAGMA busy_timeout = 10000")
        cx.execute("PRAGMA journal_mode = WAL")
        cx.execute("PRAGMA synchronous = FULL")
        cx.execute(
            """
            CREATE TABLE IF NOT EXISTS lignes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                utilisateur TEXT NOT NULL,
                role       TEXT NOT NULL,
                texte      TEXT NOT NULL,
                horodatage REAL NOT NULL
            )
            """
        )
        # ⚠ L'index porte sur (utilisateur, id), pas sur `utilisateur` seul :
        #   toutes les lectures sont « les N dernières lignes DE CET utilisateur »,
        #   donc triées par id. Sans la seconde colonne, SQLite trierait en mémoire.
        cx.execute("CREATE INDEX IF NOT EXISTS idx_util_id ON lignes(utilisateur, id)")
        _migrer_schema(cx)
        _securiser_sidecars_sqlite()
        return cx
    except Exception:
        if cx is not None:
            cx.close()
        raise


def identite(entetes: Any) -> str | None:
    """Return a stable key only after cryptographic request authentication."""

    principal = resolve_request_principal(entetes)
    return principal.conversation_key if principal is not None else None


def lire_strict(utilisateur: str, limite: int = 200) -> list[dict[str, Any]]:
    """Read history without ever returning half of a durable conversation turn."""

    try:
        with _verrou, contextlib.closing(_connexion()) as cx, cx:
            rangs = cx.execute(
                "SELECT role, texte, horodatage, turn_id, turn_position FROM lignes"
                " WHERE utilisateur = ? ORDER BY id DESC LIMIT ?",
                (utilisateur, limite),
            ).fetchall()
            visible_turn_ids = sorted(
                {str(row[3]) for row in rangs if row[3] is not None}
            )
            journaux: list[tuple[str, str | None, str | None, str | None, str]] = []
            if visible_turn_ids:
                placeholders = ",".join("?" for _turn_id in visible_turn_ids)
                journaux = cx.execute(
                    "SELECT turn_id, assistant_text, response_json, "
                    "request_sha256, state FROM tours "
                    "WHERE utilisateur = ? "
                    f"AND turn_id IN ({placeholders})",
                    (utilisateur, *visible_turn_ids),
                ).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation history read failed") from exc
    # A manually reconciled/imported pair has no request fingerprint and remains
    # valid without an OpenAI replay envelope. A model-generated durable turn is
    # different: its fingerprint proves that it names a generation effect, so a
    # missing, corrupt or non-stop envelope must quarantine it exactly like
    # ``finish_reason=length``. Missing journal rows and non-completed states are
    # also invisible rather than being inferred complete from the two text rows.
    tours_persistes_complets = {
        str(turn_id)
        for turn_id, assistant_text, response_json, request_sha256, state in journaux
        if state == "completed"
        and assistant_text is not None
        and (
            request_sha256 is None
            or (
                response_json is not None
                and _response_envelope_is_complete(response_json, assistant_text)
            )
        )
    }
    tours_incomplets = set(visible_turn_ids) - tours_persistes_complets
    if tours_incomplets:
        logger.warning(
            "conversation history quarantined %d incomplete durable turn(s)",
            len(tours_incomplets),
        )
    positions_par_tour: dict[str, set[tuple[str, int | None]]] = {}
    for role, _texte, _horodatage, turn_id, turn_position in rangs:
        if turn_id is not None:
            positions_par_tour.setdefault(turn_id, set()).add((role, turn_position))
    tours_complets = {
        turn_id
        for turn_id, positions in positions_par_tour.items()
        if positions == {("user", 0), ("assistant", 1)}
    }
    return [
        {"role": r, "texte": t, "horodatage": h, "turn_id": turn_id}
        # DESC applique la limite; reversed restaure l'ordre chronologique.
        for r, t, h, turn_id, _turn_position in reversed(rangs)
        # Une limite peut couper juste entre les deux lignes atomiques d'un tour.
        # Dans ce cas on retire la moitie visible au lieu de rejouer une reponse
        # orpheline ou une question sans reponse dans le contexte du modele.
        if turn_id is None
        or (turn_id in tours_complets and turn_id in tours_persistes_complets)
    ]


def lire(utilisateur: str, limite: int = 200) -> list[dict[str, Any]]:
    """Best-effort read for legacy in-process callers."""

    try:
        return lire_strict(utilisateur, limite)
    except ConversationStorageError as exc:
        logger.warning("lecture de conversation impossible: %s", exc)
        return []


# ⚠ LISTE BLANCHE DES RÔLES — sans elle, un client pouvait écrire une ligne
#   `role: "system"` au contenu arbitraire. Comme l'historique est REJOUÉ dans le
#   contexte du modèle à chaque tour, c'était une **injection de prompt persistante** :
#   une consigne posée une fois, respectée indéfiniment. Trouvé en revue adversariale.
ROLES_ADMIS = frozenset({"user", "assistant"})

# ⚠ Bornes de taille. `MAX_LIGNES` compte des LIGNES, pas des octets : 2000 lignes de
#   10 Mio feraient conserver ~20 Gio, et le plafond ne s'y opposerait pas. Contraste
#   relevé en revue : la route `speak` du même fichier borne déjà ses entrées à 500
#   caractères, celle-ci ne bornait rien.
# Four characters per allowed API completion token give an explicit 128 KiB
# envelope. If the backend still returns more, the HTTP route records a terminal
# abandonment rather than leaving its pre-effect reservation pending forever.
MAX_CAR_TEXTE = 4 * MAX_COMPLETION_TOKENS
MAX_RESPONSE_JSON_BYTES = 2 * MAX_CAR_TEXTE
MAX_LIGNES_PAR_ENVOI = 50


def _taille_utf8(texte: str) -> int:
    return len(texte.encode("utf-8"))


def _borner_utf8(texte: str) -> str:
    brut = texte.encode("utf-8")
    if len(brut) <= MAX_CAR_TEXTE:
        return texte
    return brut[:MAX_CAR_TEXTE].decode("utf-8", errors="ignore")


def _appliquer_plafond(cx: sqlite3.Connection, utilisateur: str) -> None:
    """Prune oldest rows without ever splitting an identified turn pair."""

    while True:
        nombre = int(
            cx.execute(
                "SELECT COUNT(*) FROM lignes WHERE utilisateur = ?", (utilisateur,)
            ).fetchone()[0]
        )
        if nombre <= MAX_LIGNES:
            return
        plus_ancienne = cx.execute(
            "SELECT id, turn_id FROM lignes WHERE utilisateur = ? ORDER BY id LIMIT 1",
            (utilisateur,),
        ).fetchone()
        if plus_ancienne is None:
            return
        ligne_id, turn_id = plus_ancienne
        if turn_id is None:
            cx.execute("DELETE FROM lignes WHERE id = ?", (ligne_id,))
        else:
            _enregistrer_tombstone(
                cx,
                utilisateur,
                str(turn_id),
                reason="retention",
            )
            cx.execute(
                "DELETE FROM lignes WHERE utilisateur = ? AND turn_id = ?",
                (utilisateur, turn_id),
            )
            cx.execute(
                "DELETE FROM tours WHERE utilisateur = ? AND turn_id = ? "
                "AND state = 'completed'",
                (utilisateur, turn_id),
            )


def _lire_tour_connexion(
    cx: sqlite3.Connection, utilisateur: str, turn_id: str
) -> ConversationTurn | None:
    rangs = cx.execute(
        "SELECT role, texte, horodatage, turn_position FROM lignes "
        "WHERE utilisateur = ? AND turn_id = ? ORDER BY turn_position",
        (utilisateur, turn_id),
    ).fetchall()
    if not rangs:
        return None
    if len(rangs) != 2:
        raise TurnCollisionError("stored turn is incomplete")
    user_row, assistant_row = rangs
    if user_row[0] != "user" or user_row[3] != 0:
        raise TurnCollisionError("stored turn has an invalid user row")
    if assistant_row[0] != "assistant" or assistant_row[3] != 1:
        raise TurnCollisionError("stored turn has an invalid assistant row")
    return ConversationTurn(
        turn_id=turn_id,
        user_text=str(user_row[1]),
        assistant_text=str(assistant_row[1]),
        timestamp=float(user_row[2]),
    )


def _lire_journal_connexion(
    cx: sqlite3.Connection, utilisateur: str, turn_id: str
) -> TurnJournalEntry | None:
    row = cx.execute(
        "SELECT user_text, assistant_text, horodatage, state, request_sha256, "
        "response_json FROM tours "
        "WHERE utilisateur = ? AND turn_id = ?",
        (utilisateur, turn_id),
    ).fetchone()
    if row is None:
        return None
    reconciliation = cx.execute(
        "SELECT reason FROM turn_reconciliations "
        "WHERE utilisateur = ? AND turn_id = ? AND action = 'abandoned'",
        (utilisateur, turn_id),
    ).fetchone()
    state = str(row[3])
    if state not in {"pending", "completed"}:
        raise TurnCollisionError("stored turn has an invalid journal state")
    assistant_text = None if row[1] is None else str(row[1])
    request_sha256 = None if row[4] is None else str(row[4])
    response_json = None if row[5] is None else str(row[5])
    if request_sha256 is not None and (
        len(request_sha256) != 64
        or any(char not in "0123456789abcdef" for char in request_sha256)
    ):
        raise TurnCollisionError("stored turn has an invalid request fingerprint")
    if state == "completed" and not assistant_text:
        raise TurnCollisionError("completed turn has no assistant content")
    if state == "completed" and request_sha256 is not None and response_json is None:
        raise TurnCollisionError("generated turn has no response envelope")
    if state == "pending" and assistant_text is not None:
        raise TurnCollisionError("pending turn already has assistant content")
    if state == "pending" and response_json is not None:
        raise TurnCollisionError("pending turn already has a response envelope")
    if state == "completed" and response_json is not None:
        try:
            _valider_response_json(response_json, assistant_text or "")
        except ValueError as exc:
            raise TurnCollisionError("stored response envelope is invalid") from exc
    if reconciliation is not None:
        if state != "pending":
            raise TurnCollisionError("completed turn cannot be marked abandoned")
        state_value: Literal["pending", "completed", "abandoned"] = "abandoned"
        abandon_reason = str(reconciliation[0])
    else:
        state_value = "pending" if state == "pending" else "completed"
        abandon_reason = None
    return TurnJournalEntry(
        turn_id=turn_id,
        user_text=str(row[0]),
        assistant_text=assistant_text,
        timestamp=float(row[2]),
        state=state_value,
        abandon_reason=abandon_reason,
        request_sha256=request_sha256,
        response_json=response_json,
    )


def _valider_question(
    turn_id: str | uuid.UUID,
    user_text: str,
    horodatage: float | None,
) -> tuple[str, str, float]:
    canonical_id = normaliser_turn_id(turn_id)
    user_text = str(user_text)
    if not user_text:
        raise ValueError("a conversation turn requires non-empty user text")
    if _taille_utf8(user_text) > MAX_CAR_TEXTE:
        raise ValueError("conversation turn text exceeds the storage limit")
    timestamp = time.time() if horodatage is None else float(horodatage)
    if not math.isfinite(timestamp):
        raise ValueError("conversation turn timestamp must be finite")
    return canonical_id, user_text, timestamp


def _valider_response_json(
    response_json: str | None,
    assistant_text: str,
) -> str | None:
    """Validate the replay envelope and bind it to the committed assistant text."""

    if response_json is None:
        return None
    serialized = str(response_json)
    if not serialized or len(serialized.encode("utf-8")) > MAX_RESPONSE_JSON_BYTES:
        raise ValueError("conversation response envelope exceeds the storage limit")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            serialized,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
        response = ChatCompletionResponse.model_validate(payload, strict=True)
        canonical_payload = response.model_dump(mode="json")
        content = response.choices[0].message.content
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("conversation response envelope is invalid") from exc
    if payload != canonical_payload:
        raise ValueError("conversation response envelope is not canonical")
    if content != assistant_text:
        raise ValueError("conversation response envelope content does not match")
    return serialized


def _response_envelope_is_complete(
    response_json: str,
    assistant_text: str | None,
) -> bool:
    """Recognize a historically committed answer only with terminal proof.

    Older daemon versions committed ``finish_reason=length`` as completed.
    Their rows remain available for forensic recovery, but must not be replayed
    to the model: doing so encourages the next answer to finish the truncated
    sentence before addressing the current user message.
    """

    if not assistant_text:
        return False
    try:
        serialized = _valider_response_json(response_json, assistant_text)
        if serialized is None:
            return False
        response = ChatCompletionResponse.model_validate_json(serialized, strict=True)
        choice = response.choices[0]
        return choice.finish_reason == "stop" and bool(
            (choice.message.content or "").strip()
        )
    except (IndexError, TypeError, ValueError):
        return False


def restaurer_response_json(response_json: str) -> ChatCompletionResponse:
    """Strictly restore one canonical envelope already committed in the journal."""

    serialized = str(response_json)
    try:
        # First obtain the expected content without relaxing the canonical
        # validation performed by the shared validator below.
        payload = json.loads(serialized)
        content = payload["choices"][0]["message"]["content"]
        _valider_response_json(serialized, content)
        return ChatCompletionResponse.model_validate(payload, strict=True)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TurnCollisionError("stored response envelope is invalid") from exc


def _journal_vers_tour(
    cx: sqlite3.Connection,
    utilisateur: str,
    entry: TurnJournalEntry,
) -> ConversationTurn:
    if entry.state != "completed" or entry.assistant_text is None:
        raise TurnCollisionError("turn has not completed")
    stored = _lire_tour_connexion(cx, utilisateur, entry.turn_id)
    if stored is None:
        raise TurnCollisionError("completed turn has no conversation pair")
    if (
        stored.user_text != entry.user_text
        or stored.assistant_text != entry.assistant_text
    ):
        raise TurnCollisionError("turn journal and conversation pair diverge")
    return stored


def lire_statut_tour(
    utilisateur: str, turn_id: str | uuid.UUID
) -> TurnJournalEntry | None:
    """Read the pre-generation journal state for one principal-scoped turn."""

    canonical_id = normaliser_turn_id(turn_id)
    try:
        with _verrou, contextlib.closing(_connexion()) as cx:
            entry = _lire_journal_connexion(cx, utilisateur, canonical_id)
            if entry is not None and entry.state == "completed":
                _journal_vers_tour(cx, utilisateur, entry)
            return entry
    except TurnCollisionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation read failed") from exc


def lire_tour(utilisateur: str, turn_id: str | uuid.UUID) -> ConversationTurn | None:
    """Read a completed turn; a pending reservation is not conversation history."""

    canonical_id = normaliser_turn_id(turn_id)
    try:
        with _verrou, contextlib.closing(_connexion()) as cx:
            entry = _lire_journal_connexion(cx, utilisateur, canonical_id)
            if entry is None or entry.state != "completed":
                return None
            return _journal_vers_tour(cx, utilisateur, entry)
    except TurnCollisionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation read failed") from exc


def reserver_tour(
    utilisateur: str,
    turn_id: str | uuid.UUID,
    user_text: str,
    *,
    request_sha256: str,
    horodatage: float | None = None,
) -> TurnReservationResult:
    """Commit an immutable pending key before any model or tool may execute."""

    canonical_id, user_text, timestamp = _valider_question(
        turn_id, user_text, horodatage
    )
    request_sha256 = str(request_sha256)
    if len(request_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in request_sha256
    ):
        raise ValueError("request_sha256 must be a lowercase SHA-256 digest")
    try:
        with _verrou, contextlib.closing(_connexion()) as cx:
            cx.execute("BEGIN IMMEDIATE")
            try:
                existing = _lire_journal_connexion(cx, utilisateur, canonical_id)
                if existing is not None:
                    if existing.state == "abandoned":
                        raise TurnCollisionError(
                            "an abandoned turn id cannot be reused"
                        )
                    if (
                        existing.user_text != user_text
                        or existing.request_sha256 != request_sha256
                    ):
                        raise TurnCollisionError(
                            "turn_id already names a different effective request"
                        )
                    cx.commit()
                    return TurnReservationResult(entry=existing, created=False)
                abandoned = cx.execute(
                    "SELECT 1 FROM turn_reconciliations "
                    "WHERE utilisateur = ? AND turn_id = ?",
                    (utilisateur, canonical_id),
                ).fetchone()
                if abandoned is not None:
                    raise TurnCollisionError("an abandoned turn id cannot be reused")
                if _tombstone_existe(cx, utilisateur, canonical_id):
                    raise TurnCollisionError("an expired turn id cannot be reused")
                _verifier_capacite_cle_tour(cx, utilisateur)
                pending_count = int(
                    cx.execute(
                        "SELECT COUNT(*) FROM tours AS t "
                        "WHERE t.utilisateur = ? AND t.state = 'pending' "
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM turn_reconciliations AS r "
                        "WHERE r.utilisateur = t.utilisateur "
                        "AND r.turn_id = t.turn_id "
                        "AND r.action = 'abandoned')",
                        (utilisateur,),
                    ).fetchone()[0]
                )
                if pending_count >= MAX_TOURS_PENDING_PAR_UTILISATEUR:
                    raise PendingTurnLimitError(
                        "pending conversation turns require reconciliation"
                    )
                cx.execute(
                    "INSERT INTO tours "
                    "(utilisateur, turn_id, user_text, assistant_text, request_sha256, "
                    "horodatage, state) VALUES (?,?,?,?,?,?,'pending')",
                    (
                        utilisateur,
                        canonical_id,
                        user_text,
                        None,
                        request_sha256,
                        timestamp,
                    ),
                )
                cx.commit()
            except Exception:
                cx.rollback()
                raise
    except (PendingTurnLimitError, TurnCollisionError, TurnKeyLimitError, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation reservation failed") from exc
    return TurnReservationResult(
        entry=TurnJournalEntry(
            turn_id=canonical_id,
            user_text=user_text,
            assistant_text=None,
            timestamp=timestamp,
            state="pending",
            request_sha256=request_sha256,
        ),
        created=True,
    )


def _inserer_paire(
    cx: sqlite3.Connection,
    utilisateur: str,
    turn_id: str,
    user_text: str,
    assistant_text: str,
    timestamp: float,
) -> None:
    cx.executemany(
        "INSERT INTO lignes "
        "(utilisateur, role, texte, horodatage, turn_id, turn_position) "
        "VALUES (?,?,?,?,?,?)",
        [
            (utilisateur, "user", user_text, timestamp, turn_id, 0),
            (utilisateur, "assistant", assistant_text, timestamp, turn_id, 1),
        ],
    )


def finaliser_tour(
    utilisateur: str,
    turn_id: str | uuid.UUID,
    user_text: str,
    assistant_text: str,
    *,
    response_json: str | None = None,
) -> TurnWriteResult:
    """Atomically turn a prior pending barrier into a completed pair."""

    canonical_id, user_text, _unused_timestamp = _valider_question(
        turn_id, user_text, None
    )
    assistant_text = str(assistant_text)
    if not assistant_text:
        raise ValueError("a conversation turn requires non-empty assistant text")
    if _taille_utf8(assistant_text) > MAX_CAR_TEXTE:
        raise ValueError("conversation turn text exceeds the storage limit")
    response_json = _valider_response_json(response_json, assistant_text)
    try:
        with _verrou, contextlib.closing(_connexion()) as cx:
            cx.execute("BEGIN IMMEDIATE")
            try:
                entry = _lire_journal_connexion(cx, utilisateur, canonical_id)
                if entry is None:
                    raise TurnCollisionError("turn was not reserved before generation")
                if entry.user_text != user_text:
                    raise TurnCollisionError(
                        "turn_id already names different user content"
                    )
                if entry.state == "abandoned":
                    raise TurnCollisionError("an abandoned turn cannot be finalized")
                if entry.request_sha256 is not None and response_json is None:
                    raise ValueError(
                        "a durable generated turn requires a replay envelope"
                    )
                if entry.state == "completed":
                    existing = _journal_vers_tour(cx, utilisateur, entry)
                    if (
                        existing.assistant_text != assistant_text
                        or entry.response_json != response_json
                    ):
                        raise TurnCollisionError(
                            "turn_id already names different assistant content"
                        )
                    cx.commit()
                    return TurnWriteResult(turn=existing, created=False)

                _inserer_paire(
                    cx,
                    utilisateur,
                    canonical_id,
                    user_text,
                    assistant_text,
                    entry.timestamp,
                )
                cx.execute(
                    "UPDATE tours SET assistant_text = ?, response_json = ?, "
                    "state = 'completed' "
                    "WHERE utilisateur = ? AND turn_id = ? AND state = 'pending'",
                    (assistant_text, response_json, utilisateur, canonical_id),
                )
                _appliquer_plafond(cx, utilisateur)
                cx.commit()
            except Exception:
                cx.rollback()
                raise
    except (TurnCollisionError, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation finalization failed") from exc
    return TurnWriteResult(
        turn=ConversationTurn(
            turn_id=canonical_id,
            user_text=user_text,
            assistant_text=assistant_text,
            timestamp=entry.timestamp,
        ),
        created=True,
    )


def abandonner_tour(
    utilisateur: str,
    turn_id: str | uuid.UUID,
    *,
    reason: str = "manual_reconciliation",
    assistant_text: str | None = None,
) -> TurnReconciliationResult:
    """Mark exactly one pending key terminal without replaying a possible effect.

    Automatic terminal failures retain the reservation so their state remains
    directly inspectable. A manual reconciliation releases it, while the immutable
    content hash, optional response hash, reason and action remain in the audit row.
    Completed conversation pairs can never be abandoned through this path.
    """

    canonical_id = normaliser_turn_id(turn_id)
    reason = str(reason).strip()
    if not reason or len(reason) > 128:
        raise ValueError("conversation abandonment reason is invalid")
    assistant_digest = (
        hashlib.sha256(str(assistant_text).encode("utf-8")).hexdigest()
        if assistant_text is not None
        else None
    )
    try:
        with _verrou, contextlib.closing(_connexion()) as cx:
            cx.execute("BEGIN IMMEDIATE")
            try:
                entry = _lire_journal_connexion(cx, utilisateur, canonical_id)
                if entry is None:
                    cx.commit()
                    return TurnReconciliationResult(
                        turn_id=canonical_id,
                        abandoned=False,
                    )
                if entry.state == "abandoned":
                    cx.commit()
                    return TurnReconciliationResult(
                        turn_id=canonical_id,
                        abandoned=False,
                    )
                if entry.state != "pending":
                    raise TurnCollisionError("only a pending turn can be abandoned")
                digest = hashlib.sha256(entry.user_text.encode("utf-8")).hexdigest()
                cx.execute(
                    "INSERT INTO turn_reconciliations "
                    "(utilisateur, turn_id, action, user_text_sha256, reason, "
                    "assistant_text_sha256, horodatage) "
                    "VALUES (?, ?, 'abandoned', ?, ?, ?, ?)",
                    (
                        utilisateur,
                        canonical_id,
                        digest,
                        reason,
                        assistant_digest,
                        time.time(),
                    ),
                )
                if reason == "manual_reconciliation":
                    deleted = cx.execute(
                        "DELETE FROM tours WHERE utilisateur = ? AND turn_id = ? "
                        "AND state = 'pending'",
                        (utilisateur, canonical_id),
                    ).rowcount
                    if deleted != 1:
                        raise TurnCollisionError(
                            "pending turn changed during reconciliation"
                        )
                else:
                    # The audit row carries the immutable identity, reason and
                    # content digests needed to block every replay.  Retaining
                    # the full 128 KiB question in a terminal reservation would
                    # let repeated model/storage failures grow the database
                    # without bound while evading the pending-turn limit.
                    compacted = cx.execute(
                        "UPDATE tours SET user_text = '', assistant_text = NULL, "
                        "response_json = NULL WHERE utilisateur = ? AND turn_id = ? "
                        "AND state = 'pending'",
                        (utilisateur, canonical_id),
                    ).rowcount
                    if compacted != 1:
                        raise TurnCollisionError(
                            "pending turn changed during reconciliation"
                        )
                cx.commit()
            except Exception:
                cx.rollback()
                raise
    except (TurnCollisionError, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation reconciliation failed") from exc
    return TurnReconciliationResult(turn_id=canonical_id, abandoned=True)


def ajouter_tour(
    utilisateur: str,
    turn_id: str | uuid.UUID,
    user_text: str,
    assistant_text: str,
    *,
    horodatage: float | None = None,
) -> TurnWriteResult:
    """Atomically persist one immutable pair, or replay the identical prior write.

    The UUID is scoped by the verified principal.  Retrying the exact same pair is a
    no-op; reusing it with different content is a conflict and never mutates storage.
    Unlike the legacy best-effort ``ajouter``, errors are surfaced so an HTTP caller
    can refuse to acknowledge a reply that was not durably committed.
    """

    canonical_id, user_text, timestamp = _valider_question(
        turn_id, user_text, horodatage
    )
    assistant_text = str(assistant_text)
    if not assistant_text:
        raise ValueError("a conversation turn requires two non-empty messages")
    if _taille_utf8(assistant_text) > MAX_CAR_TEXTE:
        raise ValueError("conversation turn text exceeds the storage limit")

    try:
        with _verrou, contextlib.closing(_connexion()) as cx:
            cx.execute("BEGIN IMMEDIATE")
            try:
                entry = _lire_journal_connexion(cx, utilisateur, canonical_id)
                if entry is not None:
                    if entry.state != "completed":
                        raise TurnCollisionError(
                            "turn_id is pending or abandoned and cannot be overwritten"
                        )
                    existing = _journal_vers_tour(cx, utilisateur, entry)
                    if (
                        existing.user_text != user_text
                        or existing.assistant_text != assistant_text
                    ):
                        raise TurnCollisionError(
                            "turn_id already names different conversation content"
                        )
                    cx.commit()
                    return TurnWriteResult(turn=existing, created=False)

                abandoned = cx.execute(
                    "SELECT 1 FROM turn_reconciliations "
                    "WHERE utilisateur = ? AND turn_id = ? AND action = 'abandoned'",
                    (utilisateur, canonical_id),
                ).fetchone()
                if abandoned is not None:
                    raise TurnCollisionError("an abandoned turn id cannot be reused")
                if _tombstone_existe(cx, utilisateur, canonical_id):
                    raise TurnCollisionError("an expired turn id cannot be reused")
                _verifier_capacite_cle_tour(cx, utilisateur)

                cx.execute(
                    "INSERT INTO tours "
                    "(utilisateur, turn_id, user_text, assistant_text, "
                    "horodatage, state) VALUES (?,?,?,?,?,'completed')",
                    (
                        utilisateur,
                        canonical_id,
                        user_text,
                        assistant_text,
                        timestamp,
                    ),
                )
                _inserer_paire(
                    cx,
                    utilisateur,
                    canonical_id,
                    user_text,
                    assistant_text,
                    timestamp,
                )
                _appliquer_plafond(cx, utilisateur)
                cx.commit()
            except Exception:
                cx.rollback()
                raise
    except (TurnCollisionError, TurnKeyLimitError, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation write failed") from exc

    return TurnWriteResult(
        turn=ConversationTurn(
            turn_id=canonical_id,
            user_text=user_text,
            assistant_text=assistant_text,
            timestamp=timestamp,
        ),
        created=True,
    )


def ajouter(utilisateur: str, lignes: list[dict[str, Any]]) -> int:
    """Ajoute des lignes et applique le plafond. Rend le nombre écrit.

    ⚠ Tolérant aux entrées mal formées PLUTÔT QUE levant : la construction du lot se
      faisait hors du `try`, si bien qu'un élément non-dict ou un horodatage textuel
      produisait un HTTP 500. Une mémoire qui refuse une ligne doit refuser la ligne,
      pas la requête.
    """
    valides: list[tuple[str, str, str, float]] = []
    for ligne in lignes[:MAX_LIGNES_PAR_ENVOI]:
        if not isinstance(ligne, dict):
            continue
        texte = _borner_utf8(str(ligne.get("texte") or ""))
        if not texte:
            continue
        role = str(ligne.get("role") or "")
        if role not in ROLES_ADMIS:
            continue
        # ⚠ `math.isfinite` : `float("NaN")` RÉUSSIT, mais SQLite stocke NaN comme NULL
        #   et la contrainte `NOT NULL` fait alors échouer TOUT l'`executemany`, qui est
        #   atomique. Une seule ligne empoisonnée effaçait donc le tour entier —
        #   question ET réponse — en rendant `{"ecrites": 0}` avec un HTTP 200.
        #   Un tour de conversation qui disparaît sans erreur visible est précisément
        #   ce que ce module doit empêcher.
        try:
            horodatage = float(ligne.get("horodatage") or time.time())
        except (TypeError, ValueError):
            horodatage = time.time()
        if not math.isfinite(horodatage):
            horodatage = time.time()
        valides.append((utilisateur, role, texte, horodatage))
    if not valides:
        return 0
    try:
        with _verrou, contextlib.closing(_connexion()) as cx, cx:
            cx.executemany(
                "INSERT INTO lignes (utilisateur, role, texte, horodatage) "
                "VALUES (?,?,?,?)",
                valides,
            )
            # ⚠ Le plafond s'applique PAR UTILISATEUR, jamais globalement : un bavard
            #   effacerait sinon la mémoire des autres. Les tours identifiés restent
            #   des paires : aucune taille impaire ne peut en conserver une moitié.
            _appliquer_plafond(cx, utilisateur)
    except Exception as exc:  # noqa: BLE001
        logger.warning("écriture de conversation impossible: %s", exc)
        return 0
    return len(valides)


def effacer(utilisateur: str) -> int:
    """Efface la conversation de CET utilisateur uniquement.

    Contrairement aux anciens appels best-effort de lecture et d'ajout, un
    effacement explicite ne peut jamais etre acquitte si SQLite ne l'a pas
    durabilise. Le client s'appuie sur cette exception pour conserver sa copie
    locale tant que la suppression serveur n'est pas confirmee.
    """
    try:
        with _verrou, contextlib.closing(_connexion()) as cx, cx:
            turn_ids = cx.execute(
                "SELECT turn_id FROM tours WHERE utilisateur = ?",
                (utilisateur,),
            ).fetchall()
            for (turn_id,) in turn_ids:
                _enregistrer_tombstone(
                    cx,
                    utilisateur,
                    str(turn_id),
                    reason="deleted",
                )
            cur = cx.execute("DELETE FROM lignes WHERE utilisateur = ?", (utilisateur,))
            # Explicit user deletion is also the reconciliation path for stale pending
            # barriers. It never affects another principal's idempotency journal.
            cx.execute("DELETE FROM tours WHERE utilisateur = ?", (utilisateur,))
            return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        raise ConversationStorageError("conversation deletion failed") from exc
