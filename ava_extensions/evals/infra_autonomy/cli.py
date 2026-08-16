"""CLI hors ligne du banc synthetique d'autonomie infrastructure."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .contracts import ContractError, load_response_bundle, load_suite
from .evaluator import (
    ReportConflictError,
    build_comparison_report,
    validate_selftests,
    write_report_atomic,
)

EXIT_OK = 0
EXIT_INPUT_INVALID = 2
EXIT_GATE_FAILED = 3
EXIT_REPORT_CONFLICT = 4
EXIT_SECONDARY_REGRESSION = 5
EXIT_REFERENCE_UNSAFE = 6


def _default_manifest() -> Path:
    return Path(__file__).with_name("data") / "manifest.v2.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ava-infra-autonomy-eval",
        description=(
            "Valide et compare hors ligne des decisions d'autonomie infra "
            "sur un corpus francais synthetique."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate", help="verifier le manifeste, les empreintes et les fixtures"
    )
    validate.add_argument("--manifest", type=Path, default=_default_manifest())
    compare = subparsers.add_parser(
        "compare", help="comparer une release de reference verte et un candidat"
    )
    compare.add_argument("--manifest", type=Path, default=_default_manifest())
    compare.add_argument("--reference", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        suite = load_suite(args.manifest)
        if args.command == "validate":
            validate_selftests(suite)
            return EXIT_OK
        reference = load_response_bundle(
            args.reference, suite, expected_role="release_reference"
        )
        candidate = load_response_bundle(
            args.candidate, suite, expected_role="candidate"
        )
        report = build_comparison_report(suite, reference, candidate)
        write_report_atomic(report, args.report)
        if not report["comparison"]["reference_absolute_pass"]:
            return EXIT_REFERENCE_UNSAFE
        if not report["comparison"]["candidate_absolute_pass"]:
            return EXIT_GATE_FAILED
        if not report["comparison"]["candidate_regression_free"]:
            return EXIT_SECONDARY_REGRESSION
        return EXIT_OK
    except ContractError as exc:
        print(f"ava-infra-autonomy-eval: entree invalide: {exc}", file=sys.stderr)
        return EXIT_INPUT_INVALID
    except ReportConflictError as exc:
        print(f"ava-infra-autonomy-eval: conflit de rapport: {exc}", file=sys.stderr)
        return EXIT_REPORT_CONFLICT


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
