"""CLI strictement hors ligne du banc relationnel Ava."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .contracts import (
    ContractError,
    load_response_bundle,
    load_review_evidence,
    load_suite,
)
from .evaluator import ReportConflictError, build_comparison_report, write_report_atomic

EXIT_OK = 0
EXIT_INPUT_INVALID = 2
EXIT_GATE_FAILED = 3
EXIT_REPORT_CONFLICT = 4
EXIT_SECONDARY_REGRESSION = 5


def _default_manifest() -> Path:
    return Path(__file__).with_name("data") / "manifest.v2.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ava-relationship-eval",
        description=(
            "Valide et compare hors ligne des reponses sur le corpus "
            "relationnel synthetique."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate", help="verifier le manifeste, ses empreintes et ses fixtures"
    )
    validate.add_argument("--manifest", type=Path, default=_default_manifest())

    compare = subparsers.add_parser(
        "compare", help="comparer une baseline et un candidat deja produits"
    )
    compare.add_argument("--manifest", type=Path, default=_default_manifest())
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--baseline-release-attestation", type=Path)
    compare.add_argument("--baseline-release-attestation-sha256")
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--candidate-release-attestation", type=Path)
    compare.add_argument("--candidate-release-attestation-sha256")
    compare.add_argument("--report", type=Path, required=True)
    compare.add_argument("--human-adjudication", type=Path)
    compare.add_argument("--human-adjudication-sha256")
    compare.add_argument("--independent-adjudication", type=Path)
    compare.add_argument("--independent-adjudication-sha256")
    compare.add_argument("--external-anchor", type=Path)
    compare.add_argument("--external-anchor-sha256")
    compare.add_argument("--anchor-public-key", type=Path)
    compare.add_argument("--anchor-public-key-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        suite = load_suite(args.manifest)
        if args.command == "validate":
            return EXIT_OK
        baseline = load_response_bundle(
            args.baseline,
            suite,
            expected_role="baseline",
            release_attestation_path=args.baseline_release_attestation,
            release_attestation_sha256=args.baseline_release_attestation_sha256,
        )
        candidate = load_response_bundle(
            args.candidate,
            suite,
            expected_role="candidate",
            release_attestation_path=args.candidate_release_attestation,
            release_attestation_sha256=args.candidate_release_attestation_sha256,
        )
        report = build_comparison_report(suite, baseline, candidate)
        review_args = (
            args.human_adjudication,
            args.human_adjudication_sha256,
            args.independent_adjudication,
            args.independent_adjudication_sha256,
            args.external_anchor,
            args.external_anchor_sha256,
            args.anchor_public_key,
            args.anchor_public_key_sha256,
        )
        if any(value is not None for value in review_args):
            if not all(value is not None for value in review_args):
                raise ContractError(
                    "les deux adjudications, leurs empreintes et l'ancrage "
                    "sont indivisibles"
                )
            evidence = load_review_evidence(
                statement_sha256=report["promotion"]["review_statement_sha256"],
                human_path=args.human_adjudication,
                human_sha256=args.human_adjudication_sha256,
                independent_path=args.independent_adjudication,
                independent_sha256=args.independent_adjudication_sha256,
                anchor_path=args.external_anchor,
                anchor_sha256=args.external_anchor_sha256,
                anchor_public_key_path=args.anchor_public_key,
                anchor_public_key_sha256=args.anchor_public_key_sha256,
                suite=suite,
                candidate_sha256=candidate.sha256,
            )
            report = build_comparison_report(
                suite,
                baseline,
                candidate,
                review_evidence=evidence,
            )
        write_report_atomic(report, args.report)
        if not report["candidate"]["gate_pass"]:
            return EXIT_GATE_FAILED
        if report["comparison"].get("candidate_regression_free") is False:
            return EXIT_SECONDARY_REGRESSION
        return EXIT_OK
    except ContractError as exc:
        print(f"ava-relationship-eval: entree invalide: {exc}", file=sys.stderr)
        return EXIT_INPUT_INVALID
    except ReportConflictError as exc:
        print(f"ava-relationship-eval: conflit de rapport: {exc}", file=sys.stderr)
        return EXIT_REPORT_CONFLICT


if __name__ == "__main__":  # pragma: no cover - couvert par __main__.py
    raise SystemExit(main())
