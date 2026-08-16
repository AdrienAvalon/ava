"""Identite fail-closed d'Ava appliquee au chargement de configuration.

OpenJarvis expose ``agent.system_prompt_path`` mais le runtime principal ne le lit pas.
Le fork utilise la persona versionnee dans ce paquet par defaut et refuse une persona
explicitement configuree qui serait absente, vide, illisible ou symbolique.
"""

from __future__ import annotations

import logging
import sys
from functools import wraps
from pathlib import Path

log = logging.getLogger(__name__)

_PATCH_MARKER = "_ava_system_prompt_patched"
_DEFAULT_PERSONA = (
    Path(__file__).resolve().parents[1] / "identity" / "system_prompts" / "ava.md"
)


def _wrapper_chain(function):  # noqa: ANN001, ANN202 - wrappers dynamiques
    current = function
    seen: set[int] = set()
    while callable(current) and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__wrapped__", None)


def _contains_patch(function) -> bool:  # noqa: ANN001
    return any(getattr(item, _PATCH_MARKER, False) for item in _wrapper_chain(function))


def _read_persona(path: Path, *, configured: bool) -> str:
    label = "configuree" if configured else "embarquee"
    if path.is_symlink():
        raise RuntimeError(f"persona Ava {label} symbolique interdite: {path}")
    try:
        if not path.is_file():
            raise RuntimeError(f"persona Ava {label} absente: {path}")
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"persona Ava {label} illisible: {path}") from exc
    if not content:
        raise RuntimeError(f"persona Ava {label} vide: {path}")
    return content


def load_common_persona() -> str:
    """Return the bundled, non-private persona exposed to every caller."""

    return _read_persona(_DEFAULT_PERSONA, configured=False)


# Valider l'artefact AVANT d'importer le package racine ``openjarvis``. Lors d'un
# import direct de ce module, cet import est réentrant ; une persona absente doit
# échouer avant qu'un OpenJarvis générique puisse rester dans ``sys.modules``.
_read_persona(_DEFAULT_PERSONA, configured=False)
_PERSONA_VALIDATED = True

from openjarvis.core import config as _cfg_mod  # noqa: E402

_original_load = _cfg_mod.load_config


def apply_identity(config):  # noqa: ANN001, ANN201 - schema upstream dynamique
    """Applique l'identite versionnee a toute configuration, meme injectee.

    ``load_config`` n'est pas la seule frontiere publique : le SDK, le builder et
    le serveur acceptent aussi un ``JarvisConfig`` construit par l'appelant. Cette
    fonction idempotente est donc le contrat canonique commun a ces chemins.
    """

    agent = getattr(config, "agent", None)
    if agent is None:
        raise RuntimeError("schema upstream inattendu: section agent absente")
    configured_path = str(getattr(agent, "system_prompt_path", "") or "").strip()
    path = Path(configured_path).expanduser() if configured_path else _DEFAULT_PERSONA
    content = _read_persona(path, configured=bool(configured_path))
    agent.default_system_prompt = content
    log.info("ava: persona chargee depuis %s (%d caracteres)", path, len(content))
    return config


@wraps(_original_load)
def _patched_load(*args, **kwargs):
    return apply_identity(_original_load(*args, **kwargs))


if not _contains_patch(_cfg_mod.load_config):
    setattr(_patched_load, _PATCH_MARKER, True)
    for attribute in ("cache_clear", "cache_info", "cache_parameters"):
        if hasattr(_original_load, attribute):
            setattr(_patched_load, attribute, getattr(_original_load, attribute))
    _cfg_mod.load_config = _patched_load


def assert_installed() -> None:
    # Revalider le contenu, pas seulement le marqueur du wrapper. Le fichier peut
    # manquer dans une wheel mal construite et une permutation d'import peut avoir vu
    # ce module avant que sa validation de niveau module soit terminée.
    _read_persona(_DEFAULT_PERSONA, configured=False)
    if not _contains_patch(_cfg_mod.load_config):
        raise RuntimeError("garde de persona Ava non installe")


assert_installed()

# Dans la permutation où ce module est importé en premier, le boot a dû différer
# cette preuve pendant notre import partiel. La rejouer avant de rendre la main ferme
# la fenêtre et compose aussi le garde d'apprentissage.
_boot = sys.modules.get("ava_extensions.boot")
if _boot is not None and bool(getattr(_boot, "boot_complete", lambda: False)()):
    _boot.finalize_security_guards()

__all__ = ["apply_identity", "assert_installed", "load_common_persona"]
