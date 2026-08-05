"""File sensitivity policy — block access to secrets, credentials, and keys."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Union

DEFAULT_SENSITIVE_PATTERNS: frozenset[str] = frozenset(
    {
        ".env",
        ".env.*",
        "*.env",
        ".secret",
        "*.secrets",
        "credentials.*",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.jks",
        "id_rsa",
        "id_ed25519",
        ".htpasswd",
        ".pgpass",
        ".netrc",
        # ⚠ MOTIFS DE JETON — ajoutes le 2026-08-05 apres mesure sur l'installation Avalon.
        #   `~/.openjarvis/cp_voice_token` etait rendu EN CLAIR par `file_read` : aucun
        #   motif amont ne couvre un fichier de jeton, qui n'a ni extension ni nom en
        #   « credentials ». Or ce jeton autorise `logs` et `proposer` cote control plane.
        #   Les trois formes couvrent les conventions rencontrees ; `*token*` est
        #   volontairement large — le cout d'un faux positif est un fichier non lu, celui
        #   d'un faux negatif est un secret recite au modele.
        "*token*",
        "*.token",
        "*_secret",
        "*apikey*",
        "*api_key*",
    }
)


def is_sensitive_file(path: Union[str, Path]) -> bool:
    """Return ``True`` if *path* matches a sensitive file pattern.

    Checks both the filename and the full name against
    ``DEFAULT_SENSITIVE_PATTERNS`` using :func:`fnmatch.fnmatch`.
    Uses the Rust implementation when available, falls back to Python.
    """
    # ⚠ UNION DES DEUX IMPLEMENTATIONS, PAS PREFERENCE DE L'UNE. Le code d'origine
    #   rendait la reponse RUST des qu'elle etait disponible, et ne tombait sur Python
    #   qu'en cas d'`ImportError`. Consequence mesuree le 2026-08-05 : un motif ajoute a
    #   `DEFAULT_SENSITIVE_PATTERNS` etait DU CODE MORT en production — la liste Python
    #   n'etait jamais consultee. On croit durcir, rien ne change, et aucune erreur ne le
    #   signale. Meme classe de defaut que partout ailleurs dans ce projet : *une source
    #   qui ne porte pas la donnee repond « rien » sans erreur.*
    # ⚠ L'UNION VA DANS LE SENS DU REFUS : un fichier est sensible si l'UNE des deux le
    #   dit. C'est le seul sens acceptable pour une garde — un desaccord doit fermer, pas
    #   ouvrir. Le cout est un appel Python supplementaire sur les seuls fichiers que Rust
    #   accepte ; le benefice est qu'un motif ajoute ici prend effet SANS recompiler.
    try:
        from openjarvis._rust_bridge import get_rust_module

        _rust = get_rust_module()
        if _rust.is_sensitive_file(str(path)):
            return True
    except (ImportError, AttributeError):
        pass
    return _is_sensitive_file_py(str(path))


def _is_sensitive_file_py(path_str: str) -> bool:
    """Pure-Python fallback for sensitive file detection."""
    import fnmatch

    p = Path(path_str)
    name = p.name
    for pattern in DEFAULT_SENSITIVE_PATTERNS:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(p), pattern):
            return True
    return False


def filter_sensitive_paths(paths: Iterable[Union[str, Path]]) -> List[Path]:
    """Return only non-sensitive paths from *paths*."""
    return [Path(p) for p in paths if not is_sensitive_file(p)]


__all__ = [
    "DEFAULT_SENSITIVE_PATTERNS",
    "filter_sensitive_paths",
    "is_sensitive_file",
]
