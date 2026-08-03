"""Security middleware -- HTTP security headers and request guards."""

from __future__ import annotations

from typing import Any

__all__ = ["CSP_POLICY", "SECURITY_HEADERS", "create_security_middleware"]


CSP_POLICY = "; ".join(
    [
        "default-src 'self'",
        "connect-src 'self' https://auth.avalon-network.com",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob: data:",
        "font-src 'self' data:",
        "frame-src 'self' https://auth.avalon-network.com",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
    ]
)


def create_security_middleware() -> Any:
    """Create a FastAPI middleware that adds security headers.

    Returns a middleware class/callable, or None if FastAPI is not available.

    Headers added:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY
    - X-XSS-Protection: 1; mode=block
    - Strict-Transport-Security: max-age=31536000; includeSubDomains
    - Referrer-Policy: strict-origin-when-cross-origin
    - Permissions-Policy: camera=(), microphone=(self), geolocation=()

    OPTIONS requests are passed through without headers so that
    CORS preflight is not blocked.
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        from starlette.responses import Response
    except ImportError:
        return None

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            # Let CORS preflight requests pass through without
            # security headers that would conflict with CORS.
            if request.method == "OPTIONS":
                return await call_next(request)

            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(self), geolocation=()"
            )
            # ⚠ ON GARDE `CSP_POLICY` (constante Ava) CONTRE LA VALEUR EN DUR DE L'AMONT.
            #   Deux raisons : une seule source pour la politique (l'amont la duplique
            #   entre le middleware et `SECURITY_HEADERS`, donc les deux peuvent diverger
            #   sans que rien ne le signale), et notre politique est plus restrictive.
            response.headers["Content-Security-Policy"] = CSP_POLICY
            return response

    return SecurityHeadersMiddleware


# Also export the header values as constants for testing
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # ⚠⚠ `microphone=(self)` EST LOAD-BEARING — l'amont met `microphone=()`, ce qui
    #    DÉSACTIVE la capture vocale du navigateur. Ava est un assistant VOCAL : reprendre
    #    la valeur amont couperait le micro sur toute l'application.
    #    Le symptôme serait trompeur au possible — le bouton réagit, aucune erreur n'est
    #    levée, `getUserMedia` échoue simplement en NotAllowedError et l'on part chercher
    #    une autorisation navigateur ou un problème de matériel.
    #    À revérifier à CHAQUE synchronisation amont : ce champ est un conflit récurrent.
    "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
    "Content-Security-Policy": CSP_POLICY,
}
