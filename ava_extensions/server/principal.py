"""Trusted request principals for Ava's HTTP chat boundary.

Two authentication mechanisms are deliberately supported and kept distinct:

* browser requests carry a Keycloak OIDC JWT in ``X-Ava-Identity``;
* the Avalon Control Plane carries a short-lived HMAC assertion in
  ``X-Ava-Service-Assertion``.

Both mechanisms fail closed.  This module never decodes an unsigned JWT for
identity, never falls back to a username/e-mail supplied by the client, and
never reads a signing key from an environment variable or command line.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

OIDC_HEADER = "X-Ava-Identity"
SERVICE_ASSERTION_HEADER = "X-Ava-Service-Assertion"

_MAX_TOKEN_BYTES = 16 * 1024
_MAX_JWKS_BYTES = 1024 * 1024
_JWKS_CACHE_SECONDS = 300.0
_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS = 5.0
_SERVICE_MAX_TTL_SECONDS = 120
_CLOCK_SKEW_SECONDS = 10
_SYNTHETIC_SERVICE_SUBJECTS = frozenset({"scheduler:ava-veille"})


@dataclass(frozen=True, slots=True)
class Principal:
    """An identity established cryptographically at the request boundary."""

    provider: Literal["oidc", "service"]
    issuer: str
    subject: str

    @property
    def conversation_key(self) -> str:
        """Stable storage key, preserving historical OIDC conversations.

        The former (unsigned) implementation stored browser conversations
        under ``sub:<subject>``.  Keeping that exact key after strengthening
        authentication preserves existing rows without migrating or copying
        private conversation content.
        """

        if self.provider == "oidc":
            return f"sub:{self.subject}"
        digest = hashlib.sha256(self.issuer.encode("utf-8")).hexdigest()[:16]
        return f"service:{digest}:{self.subject}"

    @property
    def provenance(self) -> str:
        """Stable pseudonymous attribution for traces.

        Trace correlation needs equality, not the raw OIDC subject or Matrix
        sender.  Keep the provider visible for diagnostics while hashing the
        complete, length-unambiguous trust tuple.  This is data minimisation,
        not a secret or an authentication primitive.
        """

        material = "\x00".join((self.provider, self.issuer, self.subject)).encode(
            "utf-8"
        )
        digest = hashlib.sha256(material).hexdigest()
        return f"principal:{self.provider}:sha256:{digest}"


@dataclass(frozen=True, slots=True)
class OIDCConfig:
    issuer: str
    audience: str
    jwks_url: str


@dataclass(frozen=True, slots=True)
class ServiceAssertionConfig:
    issuer: str
    audience: str
    key_file: Path
    key_id: str = "current"
    previous_key_file: Path | None = None
    previous_key_id: str | None = None
    max_ttl_seconds: int = _SERVICE_MAX_TTL_SECONDS
    clock_skew_seconds: int = _CLOCK_SKEW_SECONDS


def _clean_identity_component(value: Any, *, maximum: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in cleaned):
        return None
    return cleaned


def _clean_key_id(value: Any) -> str | None:
    """Return a bounded, log-safe key identifier (never key material)."""

    cleaned = _clean_identity_component(value, maximum=64)
    if (
        cleaned is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cleaned) is None
    ):
        return None
    return cleaned


def _valid_service_subject(value: str) -> bool:
    """Accept Matrix principals plus an explicit closed synthetic-service set."""

    return bool(
        re.fullmatch(r"matrix:@[^:\s]+:[^\s]+", value)
        or value in _SYNTHETIC_SERVICE_SUBJECTS
    )


def _https_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
    )


def oidc_config_from_env() -> OIDCConfig | None:
    """Load an explicit OIDC trust contract, or return ``None``.

    Issuer and audience have no defaults: an accidentally incomplete runtime
    configuration must disable OIDC identity, not broaden whom Ava trusts.
    The JWKS endpoint may be derived from the already trusted issuer.
    """

    issuer = (os.getenv("AVA_OIDC_ISSUER") or "").strip().rstrip("/")
    audience = (os.getenv("AVA_OIDC_AUDIENCE") or "").strip()
    if (
        not issuer
        or _clean_identity_component(audience) is None
        or not _https_url(issuer)
    ):
        return None
    jwks_url = (os.getenv("AVA_OIDC_JWKS_URL") or "").strip()
    if not jwks_url:
        jwks_url = f"{issuer}/protocol/openid-connect/certs"
    if not _https_url(jwks_url):
        return None
    return OIDCConfig(issuer=issuer, audience=audience, jwks_url=jwks_url)


def service_assertion_config_from_env() -> ServiceAssertionConfig | None:
    """Load the CP assertion contract without reading the key itself."""

    raw_path = (os.getenv("AVA_CP_ASSERTION_KEY_FILE") or "").strip()
    if not raw_path:
        return None
    issuer = (os.getenv("AVA_CP_ASSERTION_ISSUER") or "avalon-control-plane").strip()
    audience = (os.getenv("AVA_CP_ASSERTION_AUDIENCE") or "ava").strip()
    key_id = (os.getenv("AVA_CP_ASSERTION_KEY_ID") or "current").strip()
    previous_path = (os.getenv("AVA_CP_ASSERTION_PREVIOUS_KEY_FILE") or "").strip()
    previous_key_id = (os.getenv("AVA_CP_ASSERTION_PREVIOUS_KEY_ID") or "").strip()
    if (
        not _clean_identity_component(issuer)
        or not _clean_identity_component(audience)
        or _clean_key_id(key_id) is None
        or bool(previous_path) != bool(previous_key_id)
        or (previous_key_id and _clean_key_id(previous_key_id) is None)
        or (previous_key_id and previous_key_id == key_id)
    ):
        return None
    return ServiceAssertionConfig(
        issuer=issuer,
        audience=audience,
        key_file=Path(raw_path),
        key_id=key_id,
        previous_key_file=Path(previous_path) if previous_path else None,
        previous_key_id=previous_key_id or None,
    )


class _JWKSCache:
    """Small process-local cache for a fixed, operator-configured JWKS URL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_forced_refresh: dict[str, float] = {}

    def get(self, url: str, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if not force_refresh:
                cached = self._entries.get(url)
                if cached is not None and cached[0] > now:
                    return cached[1]
            else:
                cached = self._entries.get(url)
                last_refresh = self._last_forced_refresh.get(url, 0.0)
                if (
                    cached is not None
                    and last_refresh + _JWKS_FORCE_REFRESH_COOLDOWN_SECONDS > now
                ):
                    return cached[1]
                # Reserve the refresh window before network I/O so concurrent
                # unknown-kid requests cannot fan out into a JWKS fetch storm.
                self._last_forced_refresh[url] = now

        with httpx.Client(timeout=5.0, follow_redirects=False) as client:
            response = client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            if len(response.content) > _MAX_JWKS_BYTES:
                raise ValueError("JWKS response too large")
            document = response.json()
        _validate_jwks_document(document)
        with self._lock:
            self._entries[url] = (now + _JWKS_CACHE_SECONDS, document)
        return document


_jwks_cache = _JWKSCache()


class _UnknownJWTKeyError(ValueError):
    """The signed token references a key absent from the current JWKS."""


def _validate_jwks_document(document: Any) -> None:
    if not isinstance(document, dict):
        raise ValueError("JWKS is not an object")
    keys = document.get("keys")
    if not isinstance(keys, list) or not keys or len(keys) > 64:
        raise ValueError("JWKS has no bounded key list")
    if not all(isinstance(item, dict) for item in keys):
        raise ValueError("JWKS contains a non-object key")


def _rsa_key_for_token(token: str, jwks: dict[str, Any]):
    import jwt

    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise ValueError("unexpected JWT algorithm")
    kid = _clean_identity_component(header.get("kid"), maximum=256)
    if kid is None:
        raise ValueError("JWT key id missing")
    matches = [
        key
        for key in jwks.get("keys", [])
        if key.get("kid") == kid
        and key.get("kty") == "RSA"
        and key.get("use", "sig") == "sig"
        and key.get("alg", "RS256") == "RS256"
    ]
    if not matches:
        raise _UnknownJWTKeyError("JWT key id is absent")
    if len(matches) != 1:
        raise ValueError("JWT key id is ambiguous")
    return jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key


def verify_oidc_token(
    token: str,
    *,
    config: OIDCConfig | None = None,
    jwks_loader: Callable[[str], dict[str, Any]] | None = None,
) -> Principal | None:
    """Verify signature and OIDC claims, returning no partial identity."""

    token = token.removeprefix("Bearer ").strip()
    if not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        return None
    config = config or oidc_config_from_env()
    if config is None:
        return None
    try:
        import jwt

        loader = jwks_loader or _jwks_cache.get
        jwks = loader(config.jwks_url)
        _validate_jwks_document(jwks)
        try:
            key = _rsa_key_for_token(token, jwks)
        except _UnknownJWTKeyError:
            # Keycloak rotations can introduce a new kid while the previous
            # JWKS is still in the five-minute cache.  Refresh exactly once;
            # signature and every claim remain subject to the same checks.
            if jwks_loader is None:
                jwks = _jwks_cache.get(config.jwks_url, force_refresh=True)
            else:
                jwks = jwks_loader(config.jwks_url)
            _validate_jwks_document(jwks)
            key = _rsa_key_for_token(token, jwks)
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=config.audience,
            issuer=config.issuer,
            leeway=_CLOCK_SKEW_SECONDS,
            options={
                "require": ["exp", "iss", "aud", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
        subject = _clean_identity_component(claims.get("sub"))
        if subject is None:
            return None
        # PyJWT validates ``nbf`` when present.  It is optional in standard
        # Keycloak ID tokens, so requiring its presence would reject valid
        # tokens; accepting a future value is nevertheless forbidden.
        return Principal(provider="oidc", issuer=config.issuer, subject=subject)
    except Exception:  # noqa: BLE001 - every malformed/unverifiable token fails closed
        logger.debug("OIDC identity verification failed", exc_info=True)
        return None


def _load_hmac_key(path: Path) -> bytes:
    """Read a bounded secret from a regular, owner-only runtime file."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("assertion key must be a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) not in {
            0o400,
            0o600,
        }:
            raise ValueError(
                "assertion key file must belong privately to the process user"
            )
        if metadata.st_size > 4096:
            raise ValueError("assertion key file is too large")
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_key = b"".join(chunks)
        if len(raw_key) != metadata.st_size or len(raw_key) > 4096:
            raise ValueError("assertion key changed while being read")
        key = raw_key.rstrip(b"\r\n")
    finally:
        os.close(descriptor)
    if len(key) < 32:
        raise ValueError("assertion key is shorter than 32 bytes")
    return key


def sign_service_assertion(
    *,
    key: bytes,
    issuer: str,
    audience: str,
    subject: str,
    issued_at: int,
    expires_at: int,
    nonce: str,
    not_before: int | None = None,
    key_id: str = "current",
) -> str:
    """Create the CP JWT contract (also used by cross-component tests)."""

    if len(key) < 32:
        raise ValueError("HMAC key is shorter than 32 bytes")
    if _clean_key_id(key_id) is None:
        raise ValueError("assertion key id is invalid")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise ValueError("assertion timestamps must be integers")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise ValueError("assertion timestamps must be integers")
    if not_before is None:
        not_before = issued_at
    if isinstance(not_before, bool) or not isinstance(not_before, int):
        raise ValueError("assertion timestamps must be integers")
    payload = {
        "aud": audience,
        "exp": expires_at,
        "iat": issued_at,
        "iss": issuer,
        "jti": nonce,
        "nbf": not_before,
        "sub": subject,
    }
    import jwt

    return jwt.encode(
        payload,
        key,
        algorithm="HS256",
        headers={"kid": key_id, "typ": "JWT"},
    )


def verify_service_assertion(
    assertion: str,
    *,
    config: ServiceAssertionConfig | None = None,
    now: int | None = None,
) -> Principal | None:
    """Verify a short-lived CP HMAC assertion and its exact audience."""

    if not assertion or len(assertion.encode("utf-8")) > _MAX_TOKEN_BYTES:
        return None
    config = config or service_assertion_config_from_env()
    if config is None:
        return None
    try:
        import jwt

        header = jwt.get_unverified_header(assertion)
        if header.get("alg") != "HS256" or header.get("typ", "JWT") != "JWT":
            raise ValueError("unexpected assertion algorithm")
        token_key_id = _clean_key_id(header.get("kid"))
        current_key_id = _clean_key_id(config.key_id)
        previous_key_id = _clean_key_id(config.previous_key_id)
        if current_key_id is None or token_key_id is None:
            raise ValueError("assertion key id is missing or invalid")
        if bool(config.previous_key_file) != bool(config.previous_key_id):
            raise ValueError("previous assertion key contract is incomplete")
        if previous_key_id is not None and previous_key_id == current_key_id:
            raise ValueError("assertion key ids are ambiguous")
        if token_key_id == current_key_id:
            key_path = config.key_file
        elif token_key_id == previous_key_id and config.previous_key_file is not None:
            key_path = config.previous_key_file
        else:
            raise ValueError("assertion key id is unknown")
        key = _load_hmac_key(key_path)
        payload = jwt.decode(
            assertion,
            key=key,
            algorithms=["HS256"],
            audience=config.audience,
            issuer=config.issuer,
            options={
                "require": ["aud", "exp", "iat", "iss", "jti", "nbf", "sub"],
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                # Validated manually below so contract tests can inject ``now``.
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
        if set(payload) != {
            "aud",
            "exp",
            "iat",
            "iss",
            "jti",
            "nbf",
            "sub",
        }:
            raise ValueError("invalid assertion payload")
        issued_at = payload["iat"]
        not_before = payload["nbf"]
        expires_at = payload["exp"]
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(not_before, int)
            or isinstance(not_before, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
        ):
            raise ValueError("invalid assertion timestamps")
        current = int(time.time()) if now is None else now
        if issued_at > current + config.clock_skew_seconds:
            raise ValueError("assertion issued in the future")
        if not_before > current + config.clock_skew_seconds:
            raise ValueError("assertion not active yet")
        if expires_at <= current - config.clock_skew_seconds:
            raise ValueError("assertion expired")
        if (
            not_before < issued_at
            or expires_at <= not_before
            or expires_at - issued_at > config.max_ttl_seconds
        ):
            raise ValueError("assertion lifetime is invalid")
        issuer = _clean_identity_component(payload["iss"])
        audience = _clean_identity_component(payload["aud"])
        subject = _clean_identity_component(payload["sub"])
        nonce = _clean_identity_component(payload["jti"], maximum=256)
        if (
            issuer != config.issuer
            or audience != config.audience
            or subject is None
            or nonce is None
            or len(nonce) < 16
            or not _valid_service_subject(subject)
        ):
            raise ValueError("assertion identity contract mismatch")
        return Principal(provider="service", issuer=issuer, subject=subject)
    except Exception:  # noqa: BLE001 - no assertion detail or secret enters logs
        logger.debug("service identity assertion verification failed", exc_info=True)
        return None


def _header(headers: Mapping[str, str] | Any, name: str) -> str:
    try:
        return str(headers.get(name) or "").strip()
    except Exception:  # noqa: BLE001 - foreign mapping implementation
        return ""


def _header_present(headers: Mapping[str, str] | Any, name: str) -> bool:
    try:
        expected = name.lower()
        return any(str(key).lower() == expected for key in headers.keys())
    except Exception:  # noqa: BLE001 - foreign mapping implementation
        return bool(_header(headers, name))


def resolve_request_principal(headers: Mapping[str, str] | Any) -> Principal | None:
    """Resolve exactly one trusted mechanism; ambiguity is rejected."""

    oidc_present = _header_present(headers, OIDC_HEADER)
    service_present = _header_present(headers, SERVICE_ASSERTION_HEADER)
    if oidc_present == service_present:
        # Neither header, or both at once (even if one is empty): no principal.
        # Accepting precedence would make proxy/header injection errors invisible.
        return None
    oidc_token = _header(headers, OIDC_HEADER)
    service_assertion = _header(headers, SERVICE_ASSERTION_HEADER)
    if oidc_token:
        return verify_oidc_token(oidc_token)
    if service_assertion:
        return verify_service_assertion(service_assertion)
    return None


__all__ = [
    "OIDCConfig",
    "OIDC_HEADER",
    "Principal",
    "SERVICE_ASSERTION_HEADER",
    "ServiceAssertionConfig",
    "resolve_request_principal",
    "sign_service_assertion",
    "verify_oidc_token",
    "verify_service_assertion",
]
