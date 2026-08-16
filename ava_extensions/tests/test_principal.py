"""Cryptographic contract tests for Ava request principals."""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ava_extensions.server import principal as auth


@pytest.fixture(scope="module")
def oidc_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"alg": "RS256", "kid": "test-key", "use": "sig"})
    return private, {"keys": [public_jwk]}


@pytest.fixture
def oidc_config() -> auth.OIDCConfig:
    return auth.OIDCConfig(
        issuer="https://issuer.example.invalid/realms/ava",
        audience="ava-browser",
        jwks_url="https://issuer.example.invalid/realms/ava/certs",
    )


def _oidc_token(
    private,
    config: auth.OIDCConfig,
    *,
    kid: str = "test-key",
    **changes,
) -> str:
    now = int(time.time())
    claims = {
        "aud": config.audience,
        "exp": now + 120,
        "iat": now - 1,
        "iss": config.issuer,
        "nbf": now - 1,
        "sub": "stable-subject",
    }
    for key, value in changes.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return jwt.encode(
        claims,
        private,
        algorithm="RS256",
        headers={"kid": kid, "typ": "JWT"},
    )


def _verify_oidc(token: str, config: auth.OIDCConfig, jwks: dict):
    return auth.verify_oidc_token(
        token,
        config=config,
        jwks_loader=lambda _url: jwks,
    )


def test_config_oidc_refuse_url_non_https_ou_avec_userinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVA_OIDC_AUDIENCE", "ava-browser")
    for issuer in (
        "http://issuer.example.invalid/realms/ava",
        "https://user@issuer.example.invalid/realms/ava",
        "https://:password@issuer.example.invalid/realms/ava",
    ):
        monkeypatch.setenv("AVA_OIDC_ISSUER", issuer)
        assert auth.oidc_config_from_env() is None


def test_oidc_verifie_signature_issuer_audience_et_sub(oidc_keys, oidc_config) -> None:
    private, jwks = oidc_keys
    principal = _verify_oidc(_oidc_token(private, oidc_config), oidc_config, jwks)
    assert principal == auth.Principal(
        provider="oidc",
        issuer=oidc_config.issuer,
        subject="stable-subject",
    )
    assert principal.conversation_key == "sub:stable-subject"
    assert principal.subject not in principal.provenance
    assert principal.issuer not in principal.provenance
    assert principal.provenance.startswith("principal:oidc:sha256:")


def test_provenance_est_stable_distincte_et_ne_change_pas_la_cle_conversation() -> None:
    first = auth.Principal(
        provider="service",
        issuer="avalon-control-plane",
        subject="matrix:@first:example.invalid",
    )
    same = auth.Principal(
        provider="service",
        issuer="avalon-control-plane",
        subject="matrix:@first:example.invalid",
    )
    other = auth.Principal(
        provider="service",
        issuer="avalon-control-plane",
        subject="matrix:@second:example.invalid",
    )

    assert first.provenance == same.provenance
    assert first.provenance != other.provenance
    assert first.subject not in first.provenance
    assert first.issuer not in first.provenance
    assert first.conversation_key.startswith("service:")
    assert first.conversation_key.endswith(":matrix:@first:example.invalid")


def test_oidc_refuse_une_signature_fausse(oidc_keys, oidc_config) -> None:
    _, jwks = oidc_keys
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _verify_oidc(_oidc_token(attacker, oidc_config), oidc_config, jwks) is None


def test_oidc_refuse_une_mauvaise_audience(oidc_keys, oidc_config) -> None:
    private, jwks = oidc_keys
    token = _oidc_token(private, oidc_config, aud="another-client")
    assert _verify_oidc(token, oidc_config, jwks) is None


def test_oidc_refuse_expiration_nbf_future_et_sub_absent(
    oidc_keys, oidc_config
) -> None:
    private, jwks = oidc_keys
    now = int(time.time())
    assert (
        _verify_oidc(_oidc_token(private, oidc_config, exp=now - 60), oidc_config, jwks)
        is None
    )
    assert (
        _verify_oidc(_oidc_token(private, oidc_config, nbf=now + 60), oidc_config, jwks)
        is None
    )
    assert (
        _verify_oidc(_oidc_token(private, oidc_config, sub=None), oidc_config, jwks)
        is None
    )


def test_oidc_refuse_algorithme_non_asymetrique(oidc_config) -> None:
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": oidc_config.audience,
            "exp": now + 60,
            "iss": oidc_config.issuer,
            "sub": "stable-subject",
        },
        b"x" * 32,
        algorithm="HS256",
        headers={"kid": "test-key"},
    )
    assert _verify_oidc(token, oidc_config, {"keys": [{"kid": "test-key"}]}) is None


def test_oidc_rotation_kid_force_un_unique_refresh_du_cache(
    monkeypatch,
    oidc_keys,
    oidc_config,
) -> None:
    _, stale_jwks = oidc_keys
    rotated_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_jwk = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(rotated_private.public_key())
    )
    rotated_jwk.update({"alg": "RS256", "kid": "rotated-key", "use": "sig"})
    refreshed_jwks = {"keys": [rotated_jwk]}

    class StaleThenFreshCache:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def get(self, _url: str, *, force_refresh: bool = False):
            self.calls.append(force_refresh)
            return refreshed_jwks if force_refresh else stale_jwks

    cache = StaleThenFreshCache()
    monkeypatch.setattr(auth, "_jwks_cache", cache)
    token = _oidc_token(rotated_private, oidc_config, kid="rotated-key")
    principal = auth.verify_oidc_token(token, config=oidc_config)
    assert principal is not None
    assert principal.subject == "stable-subject"
    assert cache.calls == [False, True]


def test_oidc_kid_inconnu_ne_boucle_jamais_sur_le_jwks(
    oidc_keys,
    oidc_config,
) -> None:
    private, jwks = oidc_keys
    calls: list[str] = []

    def still_stale(url: str):
        calls.append(url)
        return jwks

    token = _oidc_token(private, oidc_config, kid="never-published-key")
    assert (
        auth.verify_oidc_token(
            token,
            config=oidc_config,
            jwks_loader=still_stale,
        )
        is None
    )
    assert calls == [oidc_config.jwks_url, oidc_config.jwks_url]


@pytest.fixture
def assertion_contract(tmp_path: Path):
    key = b"test-only-cross-component-hmac-key-32-bytes-minimum"
    key_file = tmp_path / "cp-assertion.key"
    key_file.write_bytes(key + b"\n")
    key_file.chmod(0o600)
    config = auth.ServiceAssertionConfig(
        issuer="avalon-control-plane",
        audience="ava",
        key_file=key_file,
        key_id="cp-v1",
    )
    return key, config


def _service_token(key: bytes, *, now: int, **changes) -> str:
    values = {
        "issuer": "avalon-control-plane",
        "audience": "ava",
        "subject": "matrix:@owner:example.invalid",
        "issued_at": now,
        "not_before": now,
        "expires_at": now + 60,
        "nonce": "0123456789abcdef",
        "key_id": "cp-v1",
    }
    values.update(changes)
    return auth.sign_service_assertion(key=key, **values)


def test_assertion_cp_hs256_croisee(assertion_contract) -> None:
    key, config = assertion_contract
    now = int(time.time())
    token = _service_token(key, now=now)
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token,
        key,
        algorithms=["HS256"],
        audience="ava",
        issuer="avalon-control-plane",
    )
    assert header["alg"] == "HS256"
    assert header["kid"] == "cp-v1"
    assert set(claims) == {"aud", "exp", "iat", "iss", "jti", "nbf", "sub"}
    principal = auth.verify_service_assertion(token, config=config, now=now)
    assert principal == auth.Principal(
        provider="service",
        issuer="avalon-control-plane",
        subject="matrix:@owner:example.invalid",
    )
    config.key_file.chmod(0o400)
    assert auth.verify_service_assertion(token, config=config, now=now) == principal


def test_assertion_cp_refuse_signature_audience_expiration_et_ttl(
    assertion_contract, tmp_path: Path
) -> None:
    key, config = assertion_contract
    now = int(time.time())
    wrong_key_file = tmp_path / "wrong.key"
    wrong_key_file.write_bytes(b"z" * 40)
    wrong_key_file.chmod(0o600)
    wrong_config = auth.ServiceAssertionConfig(
        issuer=config.issuer,
        audience=config.audience,
        key_file=wrong_key_file,
        key_id=config.key_id,
    )
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now), config=wrong_config, now=now
        )
        is None
    )
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now, audience="not-ava"), config=config, now=now
        )
        is None
    )
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now - 180, expires_at=now - 60),
            config=config,
            now=now,
        )
        is None
    )
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now, expires_at=now + 121),
            config=config,
            now=now,
        )
        is None
    )


def test_assertion_cp_accepte_le_scheduler_explicitement_borne(
    assertion_contract,
) -> None:
    key, config = assertion_contract
    now = int(time.time())
    token = _service_token(key, now=now, subject="scheduler:ava-veille")

    assert auth.verify_service_assertion(
        token, config=config, now=now
    ) == auth.Principal(
        provider="service",
        issuer="avalon-control-plane",
        subject="scheduler:ava-veille",
    )


def test_assertion_cp_refuse_nbf_sujet_hors_grammaire_et_cle_permissive(
    assertion_contract,
) -> None:
    key, config = assertion_contract
    now = int(time.time())
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now, not_before=now + 60),
            config=config,
            now=now,
        )
        is None
    )
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now, subject="owner"), config=config, now=now
        )
        is None
    )
    for subject in (
        "scheduler:ava-chat",
        "scheduler:ava-veille:admin",
        "scheduler:*",
        "matrix:owner",
    ):
        assert (
            auth.verify_service_assertion(
                _service_token(key, now=now, subject=subject),
                config=config,
                now=now,
            )
            is None
        )
    config.key_file.chmod(0o644)
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now), config=config, now=now
        )
        is None
    )
    config.key_file.chmod(0o700)
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now), config=config, now=now
        )
        is None
    )


def test_assertion_cp_refuse_une_cle_possedee_par_un_autre_uid(
    assertion_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, config = assertion_contract
    now = int(time.time())
    monkeypatch.setattr(auth.os, "geteuid", lambda: config.key_file.stat().st_uid + 1)
    assert (
        auth.verify_service_assertion(
            _service_token(key, now=now),
            config=config,
            now=now,
        )
        is None
    )


def test_assertion_cp_accepte_cle_precedente_pendant_rotation(
    assertion_contract,
    tmp_path: Path,
) -> None:
    current_key, base_config = assertion_contract
    previous_key = b"previous-test-only-hmac-key-at-least-32-bytes"
    previous_file = tmp_path / "previous.key"
    previous_file.write_bytes(previous_key)
    previous_file.chmod(0o600)
    config = auth.ServiceAssertionConfig(
        issuer=base_config.issuer,
        audience=base_config.audience,
        key_file=base_config.key_file,
        key_id="cp-v2",
        previous_key_file=previous_file,
        previous_key_id="cp-v1",
    )
    now = int(time.time())
    current = _service_token(current_key, now=now, key_id="cp-v2")
    previous = _service_token(previous_key, now=now, key_id="cp-v1")

    assert auth.verify_service_assertion(current, config=config, now=now) is not None
    assert auth.verify_service_assertion(previous, config=config, now=now) is not None
    assert (
        auth.verify_service_assertion(
            _service_token(previous_key, now=now, key_id="unknown"),
            config=config,
            now=now,
        )
        is None
    )


def test_assertion_cp_refuse_kid_absent_ou_keyring_ambigu(
    assertion_contract,
) -> None:
    key, config = assertion_contract
    now = int(time.time())
    token = _service_token(key, now=now)
    claims = jwt.decode(token, options={"verify_signature": False})
    sans_kid = jwt.encode(claims, key, algorithm="HS256", headers={"typ": "JWT"})
    assert auth.verify_service_assertion(sans_kid, config=config, now=now) is None

    ambiguous = auth.ServiceAssertionConfig(
        issuer=config.issuer,
        audience=config.audience,
        key_file=config.key_file,
        key_id="same",
        previous_key_file=config.key_file,
        previous_key_id="same",
    )
    ambiguous_token = _service_token(key, now=now, key_id="same")
    assert (
        auth.verify_service_assertion(ambiguous_token, config=ambiguous, now=now)
        is None
    )


def test_config_assertion_rotation_est_complete_et_explicite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current = tmp_path / "current.key"
    previous = tmp_path / "previous.key"
    monkeypatch.setenv("AVA_CP_ASSERTION_KEY_FILE", str(current))
    monkeypatch.setenv("AVA_CP_ASSERTION_KEY_ID", "cp-v2")
    monkeypatch.setenv("AVA_CP_ASSERTION_PREVIOUS_KEY_FILE", str(previous))
    monkeypatch.delenv("AVA_CP_ASSERTION_PREVIOUS_KEY_ID", raising=False)
    assert auth.service_assertion_config_from_env() is None
    monkeypatch.setenv("AVA_CP_ASSERTION_PREVIOUS_KEY_ID", "cp-v1")
    config = auth.service_assertion_config_from_env()
    assert config is not None
    assert config.key_id == "cp-v2"
    assert config.previous_key_file == previous
    assert config.previous_key_id == "cp-v1"


def test_resolveur_refuse_anonyme_et_deux_mecanismes(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "verify_oidc_token",
        lambda _token: auth.Principal("oidc", "https://issuer.invalid", "subject"),
    )
    monkeypatch.setattr(
        auth,
        "verify_service_assertion",
        lambda _token: auth.Principal(
            "service", "avalon-control-plane", "matrix:@owner:example.invalid"
        ),
    )
    assert auth.resolve_request_principal({}) is None
    oidc = auth.resolve_request_principal({auth.OIDC_HEADER: "token"})
    assert oidc is not None and oidc.provider == "oidc"
    assert (
        service := auth.resolve_request_principal(
            {auth.SERVICE_ASSERTION_HEADER: "assertion"}
        )
    )
    assert service.provider == "service"
    assert (
        auth.resolve_request_principal(
            {auth.OIDC_HEADER: "token", auth.SERVICE_ASSERTION_HEADER: "assertion"}
        )
        is None
    )
    assert (
        auth.resolve_request_principal(
            {auth.OIDC_HEADER: "token", auth.SERVICE_ASSERTION_HEADER: ""}
        )
        is None
    )
