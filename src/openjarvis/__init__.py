"""OpenJarvis — modular AI assistant backend with composable intelligence primitives."""

from __future__ import annotations


# --- Ava extensions hook (fork-local, optionnel) ---
# Importé au tout début pour que les patches soient appliqués avant tout
# import de openjarvis.core.config, openjarvis.sdk, etc.
try:
    import ava_extensions.boot as _ava_boot  # noqa: F401
except ImportError:
    pass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from openjarvis.sdk import Jarvis, JarvisSystem, MemoryHandle, SystemBuilder

try:
    __version__ = _pkg_version("openjarvis")
except PackageNotFoundError:  # pragma: no cover — uninstalled source tree
    __version__ = "0.0.0+unknown"

__all__ = ["Jarvis", "JarvisSystem", "MemoryHandle", "SystemBuilder", "__version__"]
