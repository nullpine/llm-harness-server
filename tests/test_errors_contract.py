"""Every error path renders the contract envelope — FastAPI's default never escapes.

`docs/API-CONTRACT.md` §1 fixes both the code list and the body shape. This file
asserts both, for every code, through a real app rather than by calling the
handlers directly: the registration is as easy to get wrong as the rendering.
"""

from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from harness_control.errors import (
    DEFAULT_STATUS,
    AppError,
    ErrorCode,
    error_response,
    register_error_handlers,
)

#: Copied by hand out of the contract. If this list and `ErrorCode` disagree, one
#: of them changed without the other, which is the bug this catches.
CONTRACT_CODES = {
    "unauthorized",
    "forbidden",
    "not_found",
    "model_not_active",
    "model_loading",
    "activation_in_progress",
    "activation_failed",
    "upstream_unavailable",
    "bad_request",
    "internal",
}


class Body(BaseModel):
    n: int


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise/{code}")
    async def _raise(code: str) -> None:
        raise AppError(ErrorCode(code), f"{code} happened", details={"code_echo": code})

    @app.get("/return/{code}")
    async def _return(code: str) -> Any:
        return error_response(ErrorCode(code), f"{code} happened")

    @app.get("/http-exception")
    async def _http_exception() -> None:
        raise HTTPException(status_code=404, detail="no such thing")

    @app.get("/http-exception-with-headers")
    async def _http_exception_headers() -> None:
        raise HTTPException(status_code=401, detail="nope", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/teapot")
    async def _teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    @app.post("/validated")
    async def _validated(body: Body) -> dict[str, int]:
        return {"n": body.n}

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("a bug slipped through")

    @app.get("/retry-after")
    async def _retry_after() -> None:
        raise AppError(
            ErrorCode.MODEL_LOADING,
            "loading",
            details={"model_id": "glm-4.7-flash"},
            headers={"Retry-After": "25"},
        )

    # Not starlette's TestClient: it now requires httpx2, and this suite already
    # needs a real server for the streaming tests (see tests/conftest.py).
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness.test") as client:
        yield client


def envelope_of(payload: Any) -> dict[str, Any]:
    """Assert the payload *is* the envelope and nothing else, then return its body."""
    assert isinstance(payload, dict)
    assert set(payload) == {"error"}, f"extra top-level keys: {sorted(payload)}"
    assert "detail" not in payload, "FastAPI's default envelope escaped"
    error = payload["error"]
    assert set(error) == {"code", "message", "details"}
    assert isinstance(error["code"], str)
    assert isinstance(error["message"], str)
    assert isinstance(error["details"], dict)
    return cast(dict[str, Any], error)


def test_the_code_enum_is_exactly_the_contracts_list() -> None:
    assert {code.value for code in ErrorCode} == CONTRACT_CODES


def test_every_code_has_a_default_status() -> None:
    assert set(DEFAULT_STATUS) == set(ErrorCode)


@pytest.mark.parametrize("code", list(ErrorCode))
async def test_every_raised_code_round_trips(client: httpx.AsyncClient, code: ErrorCode) -> None:
    response = await client.get(f"/raise/{code.value}")
    assert response.status_code == DEFAULT_STATUS[code]
    error = envelope_of(response.json())
    assert error["code"] == code.value
    assert error["message"] == f"{code.value} happened"
    assert error["details"] == {"code_echo": code.value}


@pytest.mark.parametrize("code", list(ErrorCode))
async def test_every_returned_code_round_trips(client: httpx.AsyncClient, code: ErrorCode) -> None:
    """error_response() is the in-route form; it must render identically."""
    response = await client.get(f"/return/{code.value}")
    assert response.status_code == DEFAULT_STATUS[code]
    assert envelope_of(response.json())["code"] == code.value


def test_an_explicit_status_overrides_the_default() -> None:
    err = AppError(ErrorCode.MODEL_NOT_ACTIVE, "m", http_status=418)
    assert err.http_status == 418
    assert err.envelope()["error"]["code"] == "model_not_active"


def test_details_default_to_an_empty_dict() -> None:
    assert AppError(ErrorCode.INTERNAL, "m").envelope()["error"]["details"] == {}


async def test_http_exceptions_become_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/http-exception")
    assert response.status_code == 404
    error = envelope_of(response.json())
    assert error["code"] == "not_found"
    assert error["message"] == "no such thing"


async def test_http_exception_headers_survive(client: httpx.AsyncClient) -> None:
    response = await client.get("/http-exception-with-headers")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert envelope_of(response.json())["code"] == "unauthorized"


async def test_an_unmapped_status_still_renders_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/teapot")
    assert response.status_code == 418
    assert envelope_of(response.json())["code"] == "internal"


async def test_a_missing_route_renders_the_envelope(client: httpx.AsyncClient) -> None:
    """The router's own 404 is the one most likely to leak {"detail": ...}."""
    response = await client.get("/no-such-route")
    assert response.status_code == 404
    assert envelope_of(response.json())["code"] == "not_found"


async def test_a_wrong_method_renders_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post("/http-exception")
    assert response.status_code == 405
    assert envelope_of(response.json())["code"] == "internal"


async def test_request_validation_renders_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post("/validated", json={"n": "not a number"})
    assert response.status_code == 422
    error = envelope_of(response.json())
    assert error["code"] == "bad_request"
    assert error["details"]["errors"], "the validation failure should be reported"


async def test_an_unhandled_exception_renders_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/boom")
    assert response.status_code == 500
    error = envelope_of(response.json())
    assert error["code"] == "internal"
    assert "bug slipped through" not in error["message"], "internals must not leak"


async def test_headers_ride_along(client: httpx.AsyncClient) -> None:
    response = await client.get("/retry-after")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "25"
    assert envelope_of(response.json())["details"] == {"model_id": "glm-4.7-flash"}
