"""Le runtime Ava ne peut pas activer l'apprentissage upstream par accident."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ava_extensions import boot
from ava_extensions.patches import learning_guard, system_prompt_loader
from openjarvis.core import config as config_module
from openjarvis.system.builder import SystemBuilder


def _config():
    return SimpleNamespace(
        learning=SimpleNamespace(
            enabled=True,
            auto_update=True,
            training_enabled=True,
            skills=SimpleNamespace(auto_optimize=True),
            spec_search=SimpleNamespace(enabled=True),
        )
    )


def test_tous_les_interrupteurs_mutants_sont_forces_a_false() -> None:
    config = _config()
    changed = learning_guard.enforce(config)

    assert set(changed) == {
        "learning.enabled",
        "learning.auto_update",
        "learning.training_enabled",
        "learning.skills.auto_optimize",
        "learning.spec_search.enabled",
    }
    assert config.learning.enabled is False
    assert config.learning.auto_update is False
    assert config.learning.training_enabled is False
    assert config.learning.skills.auto_optimize is False
    assert config.learning.spec_search.enabled is False


def test_le_builder_reste_neutralise_meme_avec_un_objet_construit_a_la_main() -> None:
    config = _config()
    assert SystemBuilder._setup_learning_orchestrator(config) is None
    assert config.learning.training_enabled is False


def test_le_patch_est_idempotent() -> None:
    before = SystemBuilder._setup_learning_orchestrator
    learning_guard._install()
    learning_guard.finalize()
    assert SystemBuilder._setup_learning_orchestrator is before
    learning_guard.assert_installed()


def test_la_chaine_finale_compose_persona_et_garde_d_apprentissage() -> None:
    wrappers = list(learning_guard._wrapper_chain(config_module.load_config))

    assert any(getattr(item, "_ava_learning_guard", False) for item in wrappers)
    assert any(getattr(item, "_ava_system_prompt_patched", False) for item in wrappers)
    assert hasattr(config_module.load_config, "cache_clear")
    learning_guard.assert_installed()


@pytest.mark.parametrize(
    "premier_import",
    (
        "openjarvis",
        "ava_extensions.boot",
        "ava_extensions.patches.learning_guard",
        "ava_extensions.patches.system_prompt_loader",
    ),
)
def test_les_ordres_d_import_preservent_identite_et_garde(
    tmp_path: Path, premier_import: str
) -> None:
    persona = tmp_path / "persona.md"
    persona.write_text("Tu es Ava, persona de test.", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[agent]\nsystem_prompt_path = "{persona}"\n\n'''
        "[learning]\n"
        "enabled = true\n"
        "auto_update = true\n"
        "training_enabled = true\n\n"
        "[learning.skills]\n"
        "auto_optimize = true\n\n"
        "[learning.spec_search]\n"
        "enabled = true\n",
        encoding="utf-8",
    )
    code = f"""
import importlib
from pathlib import Path

importlib.import_module({premier_import!r})
from openjarvis import sdk
from openjarvis.core import config as config_module
from openjarvis.system import builder

loaded = config_module.load_config(Path({str(config)!r}))
assert loaded.agent.default_system_prompt == "Tu es Ava, persona de test."
assert loaded.learning.enabled is False
assert loaded.learning.auto_update is False
assert loaded.learning.training_enabled is False
assert loaded.learning.skills.auto_optimize is False
assert loaded.learning.spec_search.enabled is False
assert sdk._config_module is config_module
assert builder._config_module is config_module
assert hasattr(config_module.load_config, "cache_clear")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AVA_PERCEPTION": "0"},
    )
    assert result.returncode == 0, result.stderr


def test_la_production_refuse_un_override_de_persona(tmp_path: Path) -> None:
    persona = tmp_path / "persona.md"
    persona.write_text("Persona mutable interdite.", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(f'[agent]\nsystem_prompt_path = "{persona}"\n', encoding="utf-8")
    code = f"""
from pathlib import Path

from openjarvis.core.config import load_config

load_config(Path({str(config)!r}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "AVA_BUNDLED_PERSONA_ONLY": "1",
            "AVA_PERCEPTION": "0",
        },
    )

    assert result.returncode != 0
    assert "override de persona Ava interdit" in result.stderr


def test_la_persona_embarquee_est_non_vide() -> None:
    contenu = system_prompt_loader._read_persona(
        system_prompt_loader._DEFAULT_PERSONA, configured=False
    )
    assert "Tu es **Ava**" in contenu


def test_un_override_vide_charge_la_persona_de_la_release(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[agent]\nsystem_prompt_path = ""\n', encoding="utf-8")
    code = f"""
from pathlib import Path

from ava_extensions.patches.system_prompt_loader import _DEFAULT_PERSONA
from openjarvis.core.config import load_config

loaded = load_config(Path({str(config)!r}))
persona = _DEFAULT_PERSONA.resolve()
assert loaded.agent.system_prompt_path == ""
assert loaded.agent.default_system_prompt == persona.read_text(encoding="utf-8").strip()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "AVA_BUNDLED_PERSONA_ONLY": "1",
            "AVA_PERCEPTION": "0",
        },
    )
    assert result.returncode == 0, result.stderr


def test_les_configurations_injectees_restent_ava_et_non_mutantes() -> None:
    code = """
from openjarvis import Jarvis, SystemBuilder
from openjarvis.core.config import JarvisConfig
def config_generique():
    config = JarvisConfig()
    config.telemetry.enabled = False
    config.traces.enabled = False
    config.analytics.enabled = False
    config.learning.enabled = True
    config.learning.training_enabled = True
    return config

jarvis = Jarvis(config=config_generique())
assert "Tu es **Ava**" in jarvis.config.agent.default_system_prompt
assert jarvis.config.learning.enabled is False
assert jarvis.config.learning.training_enabled is False
jarvis.close()

builder = SystemBuilder(config=config_generique())
assert "Tu es **Ava**" in builder._config.agent.default_system_prompt
assert builder._config.learning.enabled is False
assert builder._config.learning.training_enabled is False
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AVA_PERCEPTION": "0"},
    )
    assert result.returncode == 0, result.stderr


def test_le_serveur_normalise_aussi_une_configuration_injectee() -> None:
    pytest.importorskip("fastapi")
    from openjarvis.core.config import JarvisConfig
    from openjarvis.server.app import create_app

    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    config.learning.enabled = True
    config.learning.training_enabled = True
    app = create_app(object(), "test", config=config)

    assert "Tu es **Ava**" in app.state.config.agent.default_system_prompt
    assert app.state.config.learning.enabled is False
    assert app.state.config.learning.training_enabled is False


def test_une_persona_configuree_absente_vide_ou_symbolique_est_refusee(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="absente"):
        system_prompt_loader._read_persona(tmp_path / "absente.md", configured=True)

    vide = tmp_path / "vide.md"
    vide.write_text("  \n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="vide"):
        system_prompt_loader._read_persona(vide, configured=True)

    cible = tmp_path / "cible.md"
    cible.write_text("Tu es Ava.", encoding="utf-8")
    lien = tmp_path / "persona.md"
    lien.symlink_to(cible)
    with pytest.raises(RuntimeError, match="symbolique"):
        system_prompt_loader._read_persona(lien, configured=True)


def test_une_derive_du_schema_upstream_est_refusee() -> None:
    with pytest.raises(RuntimeError, match="section learning absente"):
        learning_guard.enforce(SimpleNamespace())
    with pytest.raises(RuntimeError, match="learning.spec_search"):
        learning_guard.enforce(
            SimpleNamespace(
                learning=SimpleNamespace(
                    enabled=False,
                    auto_update=False,
                    training_enabled=False,
                    skills=SimpleNamespace(auto_optimize=False),
                )
            )
        )


def test_le_chargeur_de_securite_refuse_un_import_casse() -> None:
    def casser() -> None:
        raise ImportError("symbole upstream renomme")

    with pytest.raises(RuntimeError, match="refuse de demarrer"):
        boot._charger_obligatoire("le garde", casser)


def test_openjarvis_refuse_de_demarrer_sans_extensions_ava() -> None:
    code = """
import importlib.abc
import sys

class BloquerAva(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ava_extensions" or fullname.startswith("ava_extensions."):
            raise ModuleNotFoundError("extensions Ava absentes", name=fullname)
        return None

sys.meta_path.insert(0, BloquerAva())
try:
    import openjarvis
except ModuleNotFoundError as exc:
    assert exc.name == "ava_extensions", exc
else:
    raise AssertionError("OpenJarvis a demarre sans les extensions obligatoires d'Ava")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_import_reentrant_ne_peut_pas_conserver_openjarvis_sans_persona() -> None:
    """Une persona absente fait échouer l'import initial ET toute reprise en cache."""
    code = """
import importlib
from pathlib import Path

real_is_file = Path.is_file

def persona_absente(path):
    if path.name == "ava.md" and "system_prompts" in path.parts:
        return False
    return real_is_file(path)

Path.is_file = persona_absente

try:
    importlib.import_module("ava_extensions.patches.system_prompt_loader")
except RuntimeError as exc:
    assert "persona" in str(exc).lower(), exc
else:
    raise AssertionError("le premier import a accepte une persona absente")

try:
    importlib.import_module("openjarvis")
except RuntimeError as exc:
    assert "persona" in str(exc).lower(), exc
else:
    raise AssertionError("openjarvis est reste utilisable depuis le cache")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AVA_PERCEPTION": "0"},
    )
    assert result.returncode == 0, result.stderr
