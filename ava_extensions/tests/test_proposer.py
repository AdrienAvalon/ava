"""Contrat plan/apply de l'outil documentaire d'Ava."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def proposer_module() -> Any:
    """Charge le module sans heurter les enregistrements du daemon de test."""
    import openjarvis.core.registry as registre

    original = registre.ToolRegistry.register
    registre.ToolRegistry.register = staticmethod(lambda _cle: lambda classe: classe)  # type: ignore[assignment]
    try:
        chemin = Path(__file__).resolve().parents[1] / "skills" / "proposer.py"
        spec = importlib.util.spec_from_file_location("_test_proposer_plan", chemin)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        registre.ToolRegistry.register = original  # type: ignore[assignment]


class _Reponse:
    def __init__(self, corps: dict[str, Any]) -> None:
        self._corps = corps

    def __enter__(self) -> _Reponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._corps).encode("utf-8")


def test_plan_et_apply_ont_des_SPECS_et_CAPACITES_distinctes(
    proposer_module: Any,
) -> None:
    from ava_extensions.tool_capabilities import DOCS_PLAN, DOCS_PROPOSE, NETWORK_FETCH

    plan = proposer_module.ProposerPlanTool()
    apply = proposer_module.ProposerTool()

    assert plan.spec.name == "proposer_plan"
    assert apply.spec.name == "proposer"
    assert plan.spec.required_capabilities == [NETWORK_FETCH, DOCS_PLAN]
    assert apply.spec.required_capabilities == [NETWORK_FETCH, DOCS_PROPOSE]
    assert plan.spec.requires_capability_policy is True
    assert apply.spec.requires_capability_policy is True
    assert plan.is_local is apply.is_local is False
    assert plan.spec.parameters is not apply.spec.parameters


def test_plan_et_apply_gardent_le_MEME_jeton_principal_et_candidat(
    proposer_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    appels: list[Any] = []

    def urlopen(requete: Any, timeout: float) -> _Reponse:
        appels.append((requete, timeout))
        if requete.full_url.endswith("/plan"):
            return _Reponse(
                {
                    "ok": True,
                    "mode": "plan",
                    "effets_externes": False,
                    "fichiers": [
                        {
                            "chemin": "docs/x.md",
                            "operation": "update",
                            "octets_avant": 6,
                            "octets_apres": 8,
                            "diff": "--- a/docs/x.md\n+++ b/docs/x.md\n",
                            "truncated": False,
                        }
                    ],
                    "resume": {},
                }
            )
        return _Reponse(
            {
                "ok": True,
                "mode": "apply",
                "effets_externes": True,
                "iid": 7,
                "url": "https://gitlab.invalid/mr/7",
                "branche": "ava/docs-x",
                "fichiers": ["docs/x.md"],
            }
        )

    monkeypatch.setattr(proposer_module, "_jeton", lambda: "jeton-de-parole")
    monkeypatch.setattr(proposer_module.urllib.request, "urlopen", urlopen)
    params = {
        "titre": "docs(x): corriger la page",
        "description": "preuve locale",
        "chemin": "docs/x.md",
        "avant": "ancien",
        "apres": "nouveau",
    }

    plan = proposer_module.ProposerPlanTool().execute(**params)
    apply = proposer_module.ProposerTool().execute(**params)

    assert plan.success is apply.success is True
    assert "MODE PLAN" in plan.content and "MODE APPLY" in apply.content
    assert [appel[0].full_url for appel in appels] == [
        f"{proposer_module.CP_BASE}/api/v1/propositions/plan",
        f"{proposer_module.CP_BASE}/api/v1/propositions",
    ]
    assert [appel[0].get_header("X-cp-voice-token") for appel in appels] == [
        "jeton-de-parole",
        "jeton-de-parole",
    ]
    assert json.loads(appels[0][0].data) == json.loads(appels[1][0].data)
    assert plan.metadata["mode"] == "plan"
    assert apply.metadata["mode"] == "apply"


def test_jeton_absent_bloque_PLAN_et_APPLY_avant_reseau(
    proposer_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proposer_module, "_jeton", lambda: "")

    def interdit(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("un jeton absent ne doit declencher aucun appel")

    monkeypatch.setattr(proposer_module.urllib.request, "urlopen", interdit)
    params = {
        "titre": "docs(x): corriger la page",
        "description": "preuve",
        "chemin": "docs/x.md",
        "contenu": "# X\n",
    }

    plan = proposer_module.ProposerPlanTool().execute(**params)
    apply = proposer_module.ProposerTool().execute(**params)

    assert plan.success is apply.success is False
    assert "jeton absent" in plan.content and "jeton absent" in apply.content


def test_plan_annonce_la_TRONCATURE_exacte_du_control_plane(
    proposer_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proposer_module, "_jeton", lambda: "jeton")
    monkeypatch.setattr(
        proposer_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Reponse(
            {
                "ok": True,
                "mode": "plan",
                "effets_externes": False,
                "fichiers": [
                    {
                        "chemin": "docs/x.md",
                        "operation": "update",
                        "octets_avant": 10,
                        "octets_apres": 10,
                        "diff": "--- a/docs/x.md\n",
                        "diff_octets_rendus": 17,
                        "diff_octets_total": 91,
                        "truncated": True,
                    }
                ],
                "resume": {},
            }
        ),
    )

    resultat = proposer_module.ProposerPlanTool().execute(
        titre="docs(x): montrer le plan",
        description="preuve",
        chemin="docs/x.md",
        contenu="# X\n",
    )

    assert resultat.success is True
    assert "DIFF TRONQUE : 17/91 octets rendus" in resultat.content
