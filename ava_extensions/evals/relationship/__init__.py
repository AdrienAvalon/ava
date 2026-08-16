"""Evaluation hors ligne de la politique relationnelle d'Ava.

Ce paquet ne genere aucune reponse et ne promeut aucune connaissance. Il compare
uniquement deux jeux de reponses fournis explicitement contre un corpus synthetique.
"""

from .contracts import ContractError, load_response_bundle, load_suite
from .evaluator import build_comparison_report, write_report_atomic

__all__ = [
    "ContractError",
    "build_comparison_report",
    "load_response_bundle",
    "load_suite",
    "write_report_atomic",
]
