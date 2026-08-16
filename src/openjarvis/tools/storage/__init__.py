"""Storage primitive — persistent searchable storage."""

from __future__ import annotations

import importlib
import logging

from openjarvis.core.registry import MemoryRegistry
from openjarvis.tools.storage._stubs import MemoryBackend, RetrievalResult
from openjarvis.tools.storage.chunking import Chunk, ChunkConfig, chunk_text
from openjarvis.tools.storage.context import ContextConfig, inject_context
from openjarvis.tools.storage.ingest import ingest_path, read_document
from openjarvis.tools.storage.sqlite import SQLiteMemory

logger = logging.getLogger(__name__)

_OPTIONAL_BACKENDS = {
    "bm25": "bm25",
    "faiss": "faiss_backend",
    "colbert": "colbert_backend",
    "hybrid": "hybrid",
    "dense": "dense",
}


def register_optional_backends(backend: str | None = None) -> None:
    """Charge seulement le backend demande, ou tous pour un inventaire explicite."""

    if not MemoryRegistry.contains("sqlite"):
        MemoryRegistry.register_value("sqlite", SQLiteMemory)
    modules = (
        tuple(_OPTIONAL_BACKENDS.values())
        if backend is None
        else (_OPTIONAL_BACKENDS[backend],)
        if backend in _OPTIONAL_BACKENDS
        else ()
    )
    for module_name in modules:
        try:
            importlib.import_module(f".{module_name}", __name__)
        except Exception as exc:  # noqa: BLE001 - chaque integration est optionnelle
            logger.warning(
                "memory backend %s indisponible (%s)",
                module_name,
                type(exc).__name__,
            )


__all__ = [
    "Chunk",
    "ChunkConfig",
    "ContextConfig",
    "MemoryBackend",
    "RetrievalResult",
    "chunk_text",
    "inject_context",
    "ingest_path",
    "read_document",
    "register_optional_backends",
]
