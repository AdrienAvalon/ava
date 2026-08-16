"""Verrou Ava contre l'activation accidentelle des optimiseurs upstream.

OpenJarvis sait construire un ``LearningOrchestrator`` sans evaluateur. Dans cet etat il
ecrit les configurations candidates avant de les mesurer et les accepte automatiquement.
Ava ne doit jamais entrer dans cette voie par une simple edition de ``config.toml``.

Ce patch force les interrupteurs upstream a ``False`` au chargement et neutralise en
profondeur le builder historique. La future boucle d'evolution d'Ava utilisera un chemin
distinct, avec corpus versionne, evaluateur independant et promotion GitOps.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any

from openjarvis.core import config as _config

logger = logging.getLogger(__name__)
_PATCH_MARKER = "_ava_learning_guard"


def _wrapper_chain(function: Callable[..., Any]) -> Iterator[Callable[..., Any]]:
    """Parcourt une chaine de wrappers sans boucler sur une chaine corrompue."""

    current: Callable[..., Any] | None = function
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        wrapped = getattr(current, "__wrapped__", None)
        current = wrapped if callable(wrapped) else None


def _contains_guard(function: Callable[..., Any]) -> bool:
    return any(getattr(item, _PATCH_MARKER, False) for item in _wrapper_chain(function))


def enforce(config: Any) -> tuple[str, ...]:
    """Desactive toutes les mutations automatiques connues et rend les ecarts."""

    learning = getattr(config, "learning", None)
    if learning is None:
        raise RuntimeError("schema upstream inattendu: section learning absente")
    required = {
        "learning": (learning, ("enabled", "auto_update", "training_enabled")),
        "learning.skills": (getattr(learning, "skills", None), ("auto_optimize",)),
        "learning.spec_search": (getattr(learning, "spec_search", None), ("enabled",)),
    }
    for section, (instance, attributes) in required.items():
        if instance is None:
            raise RuntimeError(f"schema upstream inattendu: section {section} absente")
        missing = [name for name in attributes if not hasattr(instance, name)]
        if missing:
            raise RuntimeError(
                f"schema upstream inattendu: {section}.{missing[0]} absent"
            )
    changed: list[str] = []
    for name in ("enabled", "auto_update", "training_enabled"):
        if bool(getattr(learning, name, False)):
            setattr(learning, name, False)
            changed.append(f"learning.{name}")
    skills = learning.skills
    if bool(skills.auto_optimize):
        skills.auto_optimize = False
        changed.append("learning.skills.auto_optimize")
    spec_search = learning.spec_search
    if bool(spec_search.enabled):
        spec_search.enabled = False
        changed.append("learning.spec_search.enabled")
    return tuple(changed)


def _install() -> None:
    """Installe le verrou leger, sans importer ``openjarvis.system``.

    Le package ``openjarvis.system`` charge des chemins lourds lorsqu'il est importe
    pendant l'initialisation racine. Le garde de configuration doit pourtant preceder
    tous les consommateurs. La neutralisation du builder est donc terminee par
    :func:`finalize` une fois le SDK importe normalement.
    """

    current_load = _config.load_config
    if not _contains_guard(current_load):

        @wraps(current_load)
        def guarded_load(*args: Any, **kwargs: Any):
            config = current_load(*args, **kwargs)
            changed = enforce(config)
            if changed:
                logger.error(
                    "ava: optimiseurs upstream refuses (%s); "
                    "politique locale fail-closed",
                    ", ".join(changed),
                )
            return config

        setattr(guarded_load, _PATCH_MARKER, True)
        for attribute in ("cache_clear", "cache_info", "cache_parameters"):
            if hasattr(current_load, attribute):
                setattr(guarded_load, attribute, getattr(current_load, attribute))
        _config.load_config = guarded_load

    assert_config_guard_installed()
    logger.info("ava: garde de configuration d'apprentissage installee")


def finalize() -> None:
    """Neutralise le builder apres son import normal et compose tous les patches.

    Le SDK et ``SystemBuilder`` resolvent ``load_config`` dynamiquement depuis le module
    canonique. On verifie cette propriete, puis on verrouille le point de creation de
    l'orchestrateur upstream.
    """

    from openjarvis.system import builder

    assert_config_guard_installed()
    current_setup = builder.SystemBuilder._setup_learning_orchestrator
    if not getattr(current_setup, _PATCH_MARKER, False):

        def disabled_setup(config: Any):
            changed = enforce(config)
            learning = getattr(config, "learning", None)
            requested = bool(getattr(learning, "training_enabled", False))
            if changed or requested:
                logger.error(
                    "ava: LearningOrchestrator upstream refuse; "
                    "evaluateur independant absent"
                )
            return None

        setattr(disabled_setup, _PATCH_MARKER, True)
        disabled_setup.__wrapped__ = current_setup  # type: ignore[attr-defined]
        builder.SystemBuilder._setup_learning_orchestrator = staticmethod(
            disabled_setup
        )

    assert_installed()
    logger.info("ava: garde d'apprentissage fail-closed installee")


def assert_config_guard_installed() -> None:
    """Verifie le verrou precoce, meme sous un autre wrapper legitime."""

    if not _contains_guard(_config.load_config):
        raise RuntimeError("garde load_config non installee")


def assert_installed() -> None:
    """Refuse toute derive de la chaine finale et des deux points de garde."""

    assert_config_guard_installed()
    builder = sys.modules.get("openjarvis.system.builder")
    sdk = sys.modules.get("openjarvis.sdk")
    if builder is None or sdk is None:
        raise RuntimeError("garde SystemBuilder non finalisee")
    if (
        getattr(builder, "_config_module", None) is not _config
        or getattr(sdk, "_config_module", None) is not _config
    ):
        raise RuntimeError("consommateur load_config non relie au module canonique")
    if not getattr(
        builder.SystemBuilder._setup_learning_orchestrator,
        _PATCH_MARKER,
        False,
    ):
        raise RuntimeError("garde SystemBuilder non installee")


try:
    _install()
    # Lors d'un import direct de ce module, ``openjarvis`` a du differer la
    # finalisation pendant que ``finalize`` n'existait pas encore. Fermer cette
    # fenetre avant de rendre la main. Si cette preuve echoue, retirer le paquet
    # racine potentiellement acheve du cache : un import suivant doit de nouveau
    # echouer ferme, jamais reutiliser un OpenJarvis partiellement garde.
    _boot = sys.modules.get("ava_extensions.boot")
    if _boot is not None and bool(getattr(_boot, "boot_complete", lambda: False)()):
        _boot.finalize_security_guards()
except Exception:
    sys.modules.pop("openjarvis", None)
    raise


__all__ = ["assert_config_guard_installed", "assert_installed", "enforce", "finalize"]
