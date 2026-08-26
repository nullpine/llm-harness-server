"""The contract's error envelope, and the only way this service reports failure.

`docs/API-CONTRACT.md` §1 says every non-2xx body is::

    {"error": {"code": "...", "message": "human readable", "details": {}}}

FastAPI's default `{"detail": ...}` must never escape, so handlers are registered
for `AppError`, `HTTPException`, and `RequestValidationError` alike.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    """Exactly the codes listed in the contract. Do not add one without a PR there."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    MODEL_NOT_ACTIVE = "model_not_active"
    MODEL_LOADING = "model_loading"
    ACTIVATION_IN_PROGRESS = "activation_in_progress"
    ACTIVATION_FAILED = "activation_failed"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    BAD_REQUEST = "bad_request"
    INTERNAL = "internal"


#: The status each code carries when the raiser does not override it.
DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.MODEL_NOT_ACTIVE: 409,
    ErrorCode.MODEL_LOADING: 503,
    ErrorCode.ACTIVATION_IN_PROGRESS: 409,
    ErrorCode.ACTIVATION_FAILED: 500,
    ErrorCode.UPSTREAM_UNAVAILABLE: 502,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.INTERNAL: 500,
}

#: Reverse map for HTTPExceptions raised by FastAPI itself (404 from the router,
#: 405 from a bad method) that never went through AppError.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.MODEL_NOT_ACTIVE,
    502: ErrorCode.UPSTREAM_UNAVAILABLE,
    503: ErrorCode.MODEL_LOADING,
}


class AppError(Exception):
    """An error that renders as the contract envelope.

    `http_status` defaults to the code's entry in `DEFAULT_STATUS`; pass it only
    where the contract asks for something different for the same code.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details or {}
        self.http_status = http_status if http_status is not None else DEFAULT_STATUS[code]
        self.headers = headers or {}

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            }
        }

    def response(self) -> JSONResponse:
        return JSONResponse(
            self.envelope(),
            status_code=self.http_status,
            headers=self.headers or None,
        )


def error_response(
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    http_status: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build an envelope response without raising — for use inside a route body."""
    return AppError(
        code, message, details=details, http_status=http_status, headers=headers
    ).response()


async def _app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    return cast(AppError, exc).response()


async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    code = _STATUS_TO_CODE.get(http_exc.status_code, ErrorCode.INTERNAL)
    detail = str(http_exc.detail) if http_exc.detail else "request failed"
    headers = dict(http_exc.headers or {})
    return AppError(code, detail, http_status=http_exc.status_code, headers=headers).response()


async def _validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    return AppError(
        ErrorCode.BAD_REQUEST,
        "request body failed validation",
        details={"errors": _jsonable_errors(cast(RequestValidationError, exc))},
        http_status=422,
    ).response()


def _jsonable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """pydantic errors carry exception objects in `ctx`; strip them so this serialises."""
    cleaned: list[dict[str, Any]] = []
    for err in exc.errors():
        item = {k: v for k, v in err.items() if k != "ctx"}
        item["loc"] = [str(part) for part in err.get("loc", ())]
        cleaned.append(item)
    return cleaned


async def _unhandled_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """Last resort: even a bug renders as the envelope, never a bare 500 page."""
    return AppError(ErrorCode.INTERNAL, "internal error").response()


def register_error_handlers(app: FastAPI) -> None:
    """Wire every failure path to the contract envelope."""
    handler: Callable[[Request, Exception], Awaitable[JSONResponse]]
    for exc_class, handler in (
        (AppError, _app_error_handler),
        (StarletteHTTPException, _http_exception_handler),
        (RequestValidationError, _validation_error_handler),
        (Exception, _unhandled_handler),
    ):
        app.add_exception_handler(exc_class, handler)
