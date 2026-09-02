"""Contrat machine-readable des gardes non proposables de la persona commune."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERSONA = ROOT / "identity/system_prompts/ava.md"
CONTRACT = ROOT / "identity/system_prompts/persona-contract.v1.json"


def test_persona_contract_covers_the_seven_runtime_boundaries() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    persona = PERSONA.read_text(encoding="utf-8")

    assert contract["schema"] == "ava-persona-contract-v1"
    assert contract["persona_path"] == "ava_extensions/identity/system_prompts/ava.md"
    fragments = contract["required_fragments"]
    assert len(fragments) == len(set(fragments)) == 7
    assert all(fragment in persona for fragment in fragments)
    assert {
        "humaine": any("humaine" in fragment for fragment in fragments),
        "overlay": any(
            "contexte relationnel" in fragment and "authentification" in fragment
            for fragment in fragments
        ),
        "preuve": any(
            "prémisse" in fragment and "preuve" in fragment for fragment in fragments
        ),
        "outil": any(
            "explication" in fragment and "outil" in fragment for fragment in fragments
        ),
        "permission": any(
            "profil de ton" in fragment and "permission" in fragment
            for fragment in fragments
        ),
        "memoire": any(
            "memory_facts.jsonl" in fragment and "quarantaine" in fragment
            for fragment in fragments
        ),
        "destructif": any(
            "destructive" in fragment and "irréversible" in fragment
            for fragment in fragments
        ),
    } == {
        boundary: True
        for boundary in (
            "humaine",
            "overlay",
            "preuve",
            "outil",
            "permission",
            "memoire",
            "destructif",
        )
    }
