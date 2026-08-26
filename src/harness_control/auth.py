"""Bearer auth on every route except `GET /healthz` (contract §1).

Constant-time comparison is a contract requirement, not a nicety: a naive `==`
leaks the key one byte at a time to anyone who can time the response.
"""

import hmac

from fastapi import Request

from harness_control.errors import AppError, ErrorCode
from harness_control.settings import Settings, get_settings

#: The only unauthenticated path. Health probes must work before a key exists,
#: which is why `/healthz` never reveals model or key information.
EXEMPT_PATHS = frozenset({"/healthz"})

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def is_exempt(path: str) -> bool:
    return path.rstrip("/") in EXEMPT_PATHS or path == "/"


def extract_bearer(header: str | None) -> str | None:
    """The token out of an `Authorization: Bearer <token>` header, or None."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def check_key(presented: str | None, expected: str) -> bool:
    """Constant-time key comparison.

    An unset server key denies everything. The alternative — an empty key matching
    an empty header — would turn a misconfigured deployment into an open one.
    """
    if not expected or presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def unauthorized(message: str) -> AppError:
    return AppError(ErrorCode.UNAUTHORIZED, message, headers=_UNAUTHORIZED_HEADERS)


async def require_api_key(request: Request) -> None:
    """FastAPI dependency. Raises the contract's 401, never FastAPI's."""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    token = extract_bearer(request.headers.get("Authorization"))
    if token is None:
        raise unauthorized("missing bearer token")
    if not check_key(token, settings.api_key):
        raise unauthorized("invalid API key")
