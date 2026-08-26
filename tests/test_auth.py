"""Bearer auth: every route but `/healthz` (contract §1, acceptance L2)."""

import httpx
import pytest

import harness_control
from conftest import API_KEY, AUTH, MODEL_ID
from harness_control.auth import check_key, extract_bearer, is_exempt
from harness_control.routes.health import CONTRACT_VERSION

#: Every authenticated route M1 ships. `/v1/chat/completions` is POST-only.
PROTECTED = [("GET", "/v1/models"), ("GET", "/admin/state"), ("POST", "/v1/chat/completions")]

CHAT_BODY = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]}


async def request(
    client: httpx.AsyncClient, method: str, path: str, **kwargs: object
) -> httpx.Response:
    if method == "POST":
        kwargs.setdefault("json", CHAT_BODY)
    return await client.request(method, path, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------- the routes


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_no_header_is_401(client: httpx.AsyncClient, method: str, path: str) -> None:
    response = await request(client, method, path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_a_wrong_key_is_401(client: httpx.AsyncClient, method: str, path: str) -> None:
    response = await request(
        client, method, path, headers={"Authorization": "Bearer wrong-key-entirely"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_the_right_key_gets_through(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await request(client, method, path, headers=AUTH)
    assert response.status_code == 200, response.text


async def test_healthz_needs_no_key(client: httpx.AsyncClient) -> None:
    """Caddy and Azure probe this before a key is configured."""
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_s"], int)
    assert body["version"] == CONTRACT_VERSION, "clients compare this for compatibility"
    assert body["service_version"] == harness_control.__version__


async def test_healthz_reveals_nothing(client: httpx.AsyncClient) -> None:
    """Contract §3: "Never reveals model or key info."""
    body = await client.get("/healthz")
    assert set(body.json()) == {"status", "version", "service_version", "uptime_s"}
    assert API_KEY not in body.text
    assert MODEL_ID not in body.text


async def test_a_401_carries_the_www_authenticate_header(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")
    assert response.headers["www-authenticate"] == "Bearer"


async def test_the_key_is_not_echoed_back(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/v1/models", headers={"Authorization": "Bearer wrong-key-entirely"}
    )
    assert "wrong-key-entirely" not in response.text


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        API_KEY,  # no scheme
        f"Basic {API_KEY}",
        f"bearer{API_KEY}",
        f"Token {API_KEY}",
    ],
)
async def test_malformed_authorization_headers_are_401(
    client: httpx.AsyncClient, header: str
) -> None:
    response = await client.get("/v1/models", headers={"Authorization": header})
    assert response.status_code == 401


async def test_the_scheme_is_case_insensitive(client: httpx.AsyncClient) -> None:
    """RFC 7235 says the scheme is case-insensitive; the token is not."""
    response = await client.get("/v1/models", headers={"Authorization": f"bearer {API_KEY}"})
    assert response.status_code == 200


async def test_a_key_with_the_right_prefix_is_still_rejected(client: httpx.AsyncClient) -> None:
    """The failure mode a naive `startswith` or a truncating compare would allow."""
    response = await client.get("/v1/models", headers={"Authorization": f"Bearer {API_KEY[:10]}"})
    assert response.status_code == 401


async def test_an_unknown_route_still_requires_nothing_but_still_404s(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/not-a-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ----------------------------------------------------------------- the unit


def test_extract_bearer() -> None:
    assert extract_bearer("Bearer abc") == "abc"
    assert extract_bearer("BEARER abc") == "abc"
    assert extract_bearer("Bearer  abc  ") == "abc"
    assert extract_bearer(None) is None
    assert extract_bearer("") is None
    assert extract_bearer("Basic abc") is None
    assert extract_bearer("Bearer") is None
    assert extract_bearer("Bearer   ") is None


def test_check_key_compares_the_whole_key() -> None:
    assert check_key("secret-value", "secret-value") is True
    assert check_key("secret-valu", "secret-value") is False
    assert check_key("secret-values", "secret-value") is False
    assert check_key("SECRET-VALUE", "secret-value") is False


def test_an_unset_server_key_denies_everything() -> None:
    """A misconfigured deployment must fail closed, not become an open one."""
    assert check_key("", "") is False
    assert check_key("anything", "") is False
    assert check_key(None, "") is False


def test_check_key_uses_a_constant_time_comparison() -> None:
    """Contract §1: "The server MUST compare keys in constant time"."""
    import inspect

    from harness_control import auth

    assert "hmac.compare_digest" in inspect.getsource(auth.check_key)
    assert "==" not in inspect.getsource(auth.check_key).split("return", 1)[1]


def test_only_healthz_is_exempt() -> None:
    assert is_exempt("/healthz")
    assert is_exempt("/healthz/")
    assert not is_exempt("/v1/models")
    assert not is_exempt("/admin/state")
    assert not is_exempt("/healthz/../admin/state")
