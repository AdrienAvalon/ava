"""File read tool — read file contents with path validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

# Maximum file size to read (1 MB)
_MAX_SIZE_BYTES = 1_048_576


#: Perimetre par defaut de `file_read` : la racine du depot d'Ava. Ce fichier vit dans
#: `<racine>/src/openjarvis/tools/`, d'ou les trois remontees.
#: ⚠ On ne met NI `~/.openjarvis` (il contient `api_key`) NI le repertoire personnel : le
#:   seul usage legitime MESURE est la lecture du depot (l'unique appel reussi sur 16
#:   lisait `CLAUDE.md`).
_DEFAUT_AUTORISE = [Path(__file__).resolve().parents[3]]


@ToolRegistry.register("file_read")
class FileReadTool(BaseTool):
    """Read file contents with optional directory restrictions."""

    tool_id = "file_read"

    def __init__(
        self,
        allowed_dirs: Optional[List[str]] = None,
    ) -> None:
        self._allowed_dirs = [Path(d).resolve() for d in (allowed_dirs or [])]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="file_read",
            # ⚠ DESCRIPTION EXPLICITE SUR LE PERIMETRE — mesure du 2026-08-06 : cet outil
            #   reussissait **1 fois sur 16**. Les 15 echecs visaient TOUS la documentation
            #   d'Avalon (`docs/compliance/cartographie-si.md`, `docs/ava-perimetre.md`...),
            #   qui n'est PAS sur cette machine. Pire, le modele reessayait le meme chemin
            #   jusqu'a QUATRE fois avant d'abandonner : il ne l'apprenait qu'en echouant.
            # ⚠ L'unique appel reussi lisait `CLAUDE.md`, dans le depot d'Ava — l'outil a
            #   donc un usage legitime et il ne faut PAS le retirer. Ce qui manquait, c'est
            #   que la description dise OU il peut lire. Une ambiguite dans un contrat
            #   d'outil se supprime ; elle ne se rattrape pas par un bon message d'erreur
            #   (le patch d'orientation existe deja et rattrapait bien — mais chaque
            #   rattrapage coute un tour).
            description=(
                "Lit un fichier PRESENT SUR LA MACHINE D'AVA (son propre depot, ses "
                "configurations, ses journaux locaux). "
                "⚠ La documentation de l'infrastructure Avalon n'est PAS sur cette machine : "
                "pour tout chemin en `docs/`, utiliser `lire_doc`, jamais cet outil."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Chemin du fichier sur la machine d'Ava. Relatif au depot "
                            "(ex. `CLAUDE.md`) ou absolu. ⚠ PAS un chemin de la "
                            "documentation Avalon (`docs/...`) — celle-ci se lit avec "
                            "`lire_doc`."
                        ),
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": ("Max lines to return (default: all)."),
                    },
                },
                "required": ["path"],
            },
            category="filesystem",
            required_capabilities=["file:read"],
            requires_capability_policy=True,
        )

    def _is_path_allowed(self, path: Path) -> bool:
        """Le chemin est-il dans un repertoire autorise ? FERME par defaut.

        ⚠ CE TEST S'OUVRAIT PAR DEFAUT, ET C'ETAIT UNE VRAIE EXPOSITION. `allowed_dirs`
          n'est renseigne nulle part (l'outil est enregistre automatiquement, sans
          arguments), donc `if not self._allowed_dirs: return True` autorisait TOUT le
          systeme de fichiers. La seule protection etait la liste noire de
          `security/file_policy.py`.
        ⚠ UNE LISTE NOIRE ENUMERE LE MAL, donc elle a toujours des trous — mesures le
          2026-08-06 sur la machine reelle : `.ssh/id_ed25519` et `.env` etaient bien
          refuses, mais **`/proc/self/environ` etait LISIBLE**, et il porte
          `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` et `OPENJARVIS_API_KEY`. La configuration
          `~/.openjarvis/config.toml` (qui contient `api_key`) passait aussi.
          Trois cles d'API lisibles par un outil que le modele peut appeler, dans une VM
          qui execute du code tiers.
        ⚠ Une liste BLANCHE enumere le bien : elle n'a pas ce mode de defaillance. Le
          defaut couvre le seul usage legitime MESURE — lire des fichiers du depot d'Ava
          (l'unique appel reussi sur 16 lisait `CLAUDE.md`). La liste noire reste active
          PAR-DESSUS : les deux se cumulent, elles ne se remplacent pas.
        """
        autorises = self._allowed_dirs or _DEFAUT_AUTORISE
        resolved = path.resolve()
        return any(resolved == d or resolved.is_relative_to(d) for d in autorises)

    def execute(self, **params: Any) -> ToolResult:
        file_path = params.get("path", "")
        if not file_path:
            return ToolResult(
                tool_name="file_read",
                content="No path provided.",
                success=False,
            )
        path = Path(file_path)
        # Block sensitive files (secrets, credentials, keys)
        from openjarvis.security.file_policy import is_sensitive_file

        if is_sensitive_file(path):
            return ToolResult(
                tool_name="file_read",
                content=f"Access denied: {file_path} is a sensitive file.",
                success=False,
            )
        if not path.exists():
            return ToolResult(
                tool_name="file_read",
                content=f"File not found: {file_path}",
                success=False,
            )
        if not path.is_file():
            return ToolResult(
                tool_name="file_read",
                content=f"Not a file: {file_path}",
                success=False,
            )
        if not self._is_path_allowed(path):
            return ToolResult(
                tool_name="file_read",
                content=f"Access denied: {file_path} is outside allowed directories.",
                success=False,
            )
        # Check size
        try:
            size = path.stat().st_size
        except OSError as exc:
            return ToolResult(
                tool_name="file_read",
                content=f"Cannot stat file: {exc}",
                success=False,
            )
        if size > _MAX_SIZE_BYTES:
            return ToolResult(
                tool_name="file_read",
                content=f"File too large: {size} bytes (max {_MAX_SIZE_BYTES}).",
                success=False,
            )
        try:
            from openjarvis._rust_bridge import get_rust_module

            _rust = get_rust_module()
            text = _rust.FileReadTool().execute(str(path))
        except ImportError:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return ToolResult(
                tool_name="file_read",
                content=f"Read error: {exc}",
                success=False,
            )
        max_lines = params.get("max_lines")
        if max_lines is not None and max_lines > 0:
            lines = text.splitlines(keepends=True)
            text = "".join(lines[:max_lines])
        return ToolResult(
            tool_name="file_read",
            content=text,
            success=True,
            metadata={"path": str(path.resolve()), "size_bytes": size},
        )


__all__ = ["FileReadTool"]
