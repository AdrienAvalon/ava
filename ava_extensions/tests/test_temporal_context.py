"""Pure contract tests for trusted Matrix temporal prompt context."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from ava_extensions.identity.temporal_context import (
    MATRIX_EPOCH_TS_MS,
    MAX_FUTURE_SKEW_MS,
    TEMPORAL_CONTEXT_MARKER,
    TemporalContextV1,
    TemporalContextValidationError,
    parse_matrix_temporal_context_v1,
    render_temporal_context_system_fragment,
)
from ava_extensions.server.principal import Principal

MATRIX = Principal(
    provider="service",
    issuer="avalon-control-plane",
    subject="matrix:@synthetic:example.invalid",
)
_DEFAULT_PAYLOAD = object()


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


CURRENT = _timestamp_ms("2026-08-17T12:00:00.123+00:00")
PREVIOUS = _timestamp_ms("2026-08-17T11:20:00+00:00")
NOW = _timestamp_ms("2026-08-17T12:01:00+00:00")


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "current_event_ts_ms": CURRENT,
        "previous_event_ts_ms": PREVIOUS,
    }
    payload.update(changes)
    return payload


def _parse(
    payload: object = _DEFAULT_PAYLOAD,
    *,
    principal: Principal | None = MATRIX,
    now_ms: int = NOW,
) -> TemporalContextV1:
    return parse_matrix_temporal_context_v1(
        _payload() if payload is _DEFAULT_PAYLOAD else payload,
        principal=principal,
        now_ms=now_ms,
    )


def test_parse_et_rendu_canonique_ne_contiennent_que_le_temps_serveur() -> None:
    context = _parse()

    assert context == TemporalContextV1(
        version=1,
        current_event_ts_ms=CURRENT,
        previous_event_ts_ms=PREVIOUS,
    )
    fragment = render_temporal_context_system_fragment(
        context,
        principal=MATRIX,
        now_ms=NOW,
    )
    assert fragment == "\n".join(
        (
            TEMPORAL_CONTEXT_MARKER,
            "current=2026-08-17T14:00:00.123+02:00[Europe/Paris]",
            "previous=2026-08-17T13:20:00.000+02:00[Europe/Paris]",
            "elapsed_ms=2400123;elapsed_iso8601=PT0H40M0.123S",
        )
    )
    assert MATRIX.subject not in fragment
    assert MATRIX.issuer not in fragment
    assert fragment.count(TEMPORAL_CONTEXT_MARKER) == 1


def test_absence_precedent_reste_explicite_et_sans_duree_inventee() -> None:
    context = _parse(_payload(previous_event_ts_ms=None))

    fragment = render_temporal_context_system_fragment(
        context,
        principal=MATRIX,
        now_ms=NOW,
    )

    assert "previous=null" in fragment
    assert "elapsed_ms=null;elapsed_iso8601=null" in fragment


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"version": 1, "current_event_ts_ms": CURRENT},
        {
            "version": 1,
            "current_event_ts_ms": CURRENT,
            "previous_event_ts_ms": PREVIOUS,
            "text": "— 39 min plus tard —",
        },
    ],
)
def test_payload_exige_un_dict_aux_cles_exactes(payload: object) -> None:
    with pytest.raises(TemporalContextValidationError):
        _parse(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("version", 1.0),
        ("version", "1"),
        ("current_event_ts_ms", True),
        ("current_event_ts_ms", float(CURRENT)),
        ("current_event_ts_ms", str(CURRENT)),
        ("previous_event_ts_ms", False),
        ("previous_event_ts_ms", float(PREVIOUS)),
        ("previous_event_ts_ms", str(PREVIOUS)),
    ],
)
def test_champs_entiers_sont_stricts_bool_inclus(field: str, value: object) -> None:
    with pytest.raises(TemporalContextValidationError):
        _parse(_payload(**{field: value}))


@pytest.mark.parametrize("now_ms", [True, float(NOW), str(NOW)])
def test_now_injecte_est_un_entier_strict(now_ms: object) -> None:
    with pytest.raises(TemporalContextValidationError):
        parse_matrix_temporal_context_v1(
            _payload(),
            principal=MATRIX,
            now_ms=now_ms,  # type: ignore[arg-type]
        )


def test_version_inconnue_est_rejetee() -> None:
    with pytest.raises(TemporalContextValidationError):
        _parse(_payload(version=2))


@pytest.mark.parametrize(
    "principal",
    [
        None,
        Principal("oidc", "https://issuer.invalid", "subject"),
        Principal("service", "avalon-control-plane", "scheduler:ava-veille"),
        Principal("service", "avalon-control-plane", "matrix:not-a-user-id"),
        Principal("service", "avalon-control-plane", "matrix:@missing-server"),
        Principal("service", "avalon-control-plane", "matrix:@user:server\x00name"),
    ],
)
def test_seul_un_principal_service_matrix_est_admis(
    principal: Principal | None,
) -> None:
    with pytest.raises(TemporalContextValidationError):
        _parse(principal=principal)


def test_une_chaine_de_provenance_ne_remplace_pas_le_principal_verifie() -> None:
    with pytest.raises(TemporalContextValidationError):
        parse_matrix_temporal_context_v1(
            _payload(),
            principal=MATRIX.provenance,  # type: ignore[arg-type]
            now_ms=NOW,
        )


@pytest.mark.parametrize("previous", [CURRENT, CURRENT + 1])
def test_precedent_doit_strictement_preceder_le_courant(previous: int) -> None:
    with pytest.raises(TemporalContextValidationError):
        _parse(_payload(previous_event_ts_ms=previous))


@pytest.mark.parametrize(
    "changes",
    [
        {"current_event_ts_ms": MATRIX_EPOCH_TS_MS - 1},
        {"previous_event_ts_ms": MATRIX_EPOCH_TS_MS - 1},
    ],
)
def test_timestamps_epoch_ou_defaut_sont_rejetes(changes: dict[str, int]) -> None:
    with pytest.raises(TemporalContextValidationError):
        _parse(_payload(**changes))


def test_borne_future_accepte_cinq_minutes_exactes_puis_refuse() -> None:
    at_limit = NOW + MAX_FUTURE_SKEW_MS
    assert (
        _parse(
            _payload(
                current_event_ts_ms=at_limit,
                previous_event_ts_ms=at_limit - 1,
            )
        ).current_event_ts_ms
        == at_limit
    )

    with pytest.raises(TemporalContextValidationError):
        _parse(
            _payload(
                current_event_ts_ms=at_limit + 1,
                previous_event_ts_ms=at_limit,
            )
        )


def test_replay_reste_deterministe_trente_jours_plus_tard() -> None:
    first = _parse()
    thirty_days_later = NOW + 30 * 86_400_000
    replayed = _parse(now_ms=thirty_days_later)

    assert replayed == first
    assert render_temporal_context_system_fragment(
        replayed,
        principal=MATRIX,
        now_ms=thirty_days_later,
    ) == render_temporal_context_system_fragment(
        first,
        principal=MATRIX,
        now_ms=NOW,
    )


def test_offsets_paris_desambiguisent_le_repli_dst() -> None:
    first_0230 = _timestamp_ms("2026-10-25T00:30:00+00:00")
    second_0230 = _timestamp_ms("2026-10-25T01:30:00+00:00")
    context = _parse(
        {
            "version": 1,
            "current_event_ts_ms": second_0230,
            "previous_event_ts_ms": first_0230,
        },
        now_ms=second_0230,
    )

    fragment = render_temporal_context_system_fragment(
        context,
        principal=MATRIX,
        now_ms=second_0230,
    )

    assert "previous=2026-10-25T02:30:00.000+02:00[Europe/Paris]" in fragment
    assert "current=2026-10-25T02:30:00.000+01:00[Europe/Paris]" in fragment
    assert "elapsed_ms=3600000;elapsed_iso8601=PT1H0M0.000S" in fragment


def test_renderer_revalide_principal_et_borne_future() -> None:
    context = _parse()
    with pytest.raises(TemporalContextValidationError):
        render_temporal_context_system_fragment(
            context,
            principal=None,
            now_ms=NOW,
        )
    with pytest.raises(TemporalContextValidationError):
        render_temporal_context_system_fragment(
            context,
            principal=MATRIX,
            now_ms=CURRENT - MAX_FUTURE_SKEW_MS - 1,
        )


def test_contexte_est_immuable() -> None:
    context = _parse()
    with pytest.raises(FrozenInstanceError):
        context.current_event_ts_ms = CURRENT + 1  # type: ignore[misc]
