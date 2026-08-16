"""Validation hors ligne d'une copie du ledger cognitif Ava."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from urllib.parse import quote

from ava_extensions.memory.governed import GovernedMemoryStore


def verify(path: str | Path) -> None:
    """Refuse une copie incoherente sans l'initialiser ni modifier ses metadonnees."""

    database = Path(path)
    if database.is_symlink() or not database.is_file():
        raise ValueError("cognition.db doit etre un fichier regulier existant")
    if any(
        Path(f"{database}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("une copie hors ligne ne doit pas avoir de sidecar SQLite")
    database_uri = (
        f"file:{quote(str(database.resolve()), safe='/')}?mode=ro&immutable=1"
    )
    with sqlite3.connect(database_uri, uri=True, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        GovernedMemoryStore._assert_database_integrity(connection)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verifier le schema et les invariants du ledger cognitif Ava"
    )
    parser.add_argument("database", type=Path)
    arguments = parser.parse_args()
    verify(arguments.database)
    return 0


if __name__ == "__main__":  # pragma: no cover - frontiere CLI
    raise SystemExit(main())
