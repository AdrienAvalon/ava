"""Contrats de chargement isole des backends memoire optionnels."""

from __future__ import annotations

from unittest.mock import patch

from openjarvis.core.registry import MemoryRegistry
from openjarvis.tools import storage


def test_un_backend_casse_ne_masque_pas_sqlite_ni_les_suivants() -> None:
    calls: list[str] = []

    def import_module(name: str, package: str):
        calls.append(name)
        if name == ".bm25":
            raise RuntimeError("backend casse")
        return object()

    with patch.object(storage.importlib, "import_module", side_effect=import_module):
        storage.register_optional_backends()

    assert calls == [f".{name}" for name in storage._OPTIONAL_BACKENDS.values()]
    assert MemoryRegistry.contains("sqlite")


def test_seul_le_backend_demande_est_importe() -> None:
    with patch.object(storage.importlib, "import_module") as importer:
        storage.register_optional_backends("faiss")

    importer.assert_called_once_with(".faiss_backend", storage.__name__)
