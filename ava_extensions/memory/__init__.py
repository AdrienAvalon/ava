"""Memoire gouvernee specifique a Ava.

Le magasin historique partage ``memory_facts.jsonl`` est en quarantaine : il reste
sauvegarde uniquement pour une future migration controlee, mais aucun chemin runtime ne
le lit ni ne l'alimente. Le nouveau ledger est volontairement separe : une sortie de
modele y entre comme candidate attribuee et ne devient jamais une verite par le seul
fait d'avoir ete generee.
"""

from ava_extensions.memory.governed import (
    Evidence,
    EvidenceKind,
    GovernedMemoryStore,
    Memory,
    MemoryKind,
    MemoryState,
    Projection,
    Sensitivity,
    VerifiedPrincipal,
    Visibility,
)

__all__ = [
    "Evidence",
    "EvidenceKind",
    "GovernedMemoryStore",
    "Memory",
    "MemoryKind",
    "MemoryState",
    "Projection",
    "Sensitivity",
    "Visibility",
    "VerifiedPrincipal",
]
