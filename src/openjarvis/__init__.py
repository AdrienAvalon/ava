"""OpenJarvis — modular AI assistant backend with composable intelligence primitives."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# --- Ava extensions hook (fork-local, obligatoire) ---
# Ava ne doit jamais demarrer comme un OpenJarvis generique si son identite ou ses
# gardes sont absents. La partie legere precede les imports SDK ; les gardes qui
# dependent du builder sont finalises apres son chargement normal.
import ava_extensions.boot as _ava_boot

from openjarvis.sdk import Jarvis, JarvisSystem, MemoryHandle, SystemBuilder

if _ava_boot.boot_complete():
    _ava_boot.finalize_security_guards()

try:
    __version__ = _pkg_version("openjarvis")
except PackageNotFoundError:  # pragma: no cover — uninstalled source tree
    __version__ = "0.0.0+unknown"

__all__ = ["Jarvis", "JarvisSystem", "MemoryHandle", "SystemBuilder", "__version__"]
