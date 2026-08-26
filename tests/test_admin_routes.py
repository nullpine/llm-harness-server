"""`/admin/state`, and the proxy's state guards.

The guards live here rather than in `test_proxy_streaming.py` because they are
about the contract's state table, not about buffering — acceptance L6 and L7:
"never a hang or a 500".
"""

import httpx
import pytest

from conftest import AUTH, MODEL_ID
from harness_control.supervisor.state import ModelState
from harness_control.supervisor.supervisor import Supervisor

CHAT_BODY = {"model": MODEL_ID, "stream": True, "messages": [{"role": "user", "content": "hi"}]}


# ------------------------------------------------------------ /admin/state


async def test_state_matches_the_contracts_shape(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/state", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "state",
        "active_model_id",
        "previous_model_id",
        "since",
        "progress_hint",
        "last_error",
        "gpu",
    }


async def test_state_reports_ready_with_the_active_model(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/state", headers=AUTH)).json()
    assert body["state"] == "ready"
    assert body["active_model_id"] == MODEL_ID
    assert body["last_error"] is None


async def test_state_reports_idle_before_anything_is_loaded(
    idle_client: httpx.AsyncClient,
) -> None:
    body = (await idle_client.get("/admin/state", headers=AUTH)).json()
    assert body["state"] == "idle"
    assert body["active_model_id"] is None


async def test_gpu_is_an_empty_list_and_that_is_fine(client: httpx.AsyncClient) -> None:
    """Acceptance L11 — the Apple Silicon case, handled without error."""
    assert (await client.get("/admin/state", headers=AUTH)).json()["gpu"] == []


async def test_state_is_cheap_enough_to_poll(client: httpx.AsyncClient) -> None:
    """The desktop app polls this every 2 s during a load. Ten calls must be quick."""
    for _ in range(10):
        assert (await client.get("/admin/state", headers=AUTH)).status_code == 200


# ------------------------------------------------------------- /v1/models


async def test_v1_models_lists_only_the_active_model(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/models", headers=AUTH)).json()
    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == [MODEL_ID]
    assert body["data"][0]["owned_by"] == "harness"
    assert body["data"][0]["object"] == "model"


async def test_v1_models_is_empty_when_nothing_is_active(idle_client: httpx.AsyncClient) -> None:
    """Contract §2: an empty list with status 200, not a 404 and not an error."""
    response = await idle_client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


# ---------------------------------------------------------- the state guards


async def test_idle_is_409_model_not_active(idle_client: httpx.AsyncClient) -> None:
    response = await idle_client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "model_not_active"
    assert error["details"]["active"] is None


async def test_error_is_409_model_not_active(
    idle_client: httpx.AsyncClient, supervisor: Supervisor
) -> None:
    supervisor._state = ModelState.ERROR  # private on purpose: reaching the state needs a failure
    response = await idle_client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_not_active"


@pytest.mark.parametrize("state", [ModelState.LOADING, ModelState.STOPPING])
async def test_loading_and_stopping_are_503_with_retry_after(
    client: httpx.AsyncClient, ready_supervisor: Supervisor, state: ModelState
) -> None:
    """Acceptance L6: 503 `model_loading` with `Retry-After` — never a hang, never a 500."""
    ready_supervisor._state = state  # private on purpose
    response = await client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_loading"
    assert int(response.headers["Retry-After"]) > 0


async def test_a_different_model_is_409_with_both_ids(client: httpx.AsyncClient) -> None:
    """Acceptance L7. The desktop app reads this as "your dropdown is stale"."""
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "model": "qwen3.8-27b"}, headers=AUTH
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "model_not_active"
    assert error["details"] == {"active": MODEL_ID, "requested": "qwen3.8-27b"}


async def test_an_unreachable_backend_is_502(
    client: httpx.AsyncClient, ready_supervisor: Supervisor
) -> None:
    """Contract §2: "If vLLM is unreachable, respond 502 with upstream_unavailable"."""
    backend = ready_supervisor.backend
    assert backend is not None
    backend._url = "http://127.0.0.1:1"  # type: ignore[attr-defined]  # private on purpose

    response = await client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


# ------------------------------------------------------------- bad requests


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": []},
        {"model": MODEL_ID},
        {"model": 42, "messages": []},
        {"model": MODEL_ID, "messages": "not a list"},
    ],
)
async def test_a_malformed_body_is_400_not_500(
    client: httpx.AsyncClient, payload: dict[str, object]
) -> None:
    response = await client.post("/v1/chat/completions", json=payload, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


async def test_non_json_is_400_not_500(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        content=b"{not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


async def test_unknown_fields_reach_the_backend_unchanged(
    client: httpx.AsyncClient, upstream: object
) -> None:
    """Contract §2: "Unknown fields are forwarded to vLLM unchanged"."""
    await client.post(
        "/v1/chat/completions",
        json={**CHAT_BODY, "stream": False, "seed": 1234, "guided_json": {"type": "object"}},
        headers=AUTH,
    )
    forwarded = upstream.chat_calls[-1]["body"]  # type: ignore[attr-defined]
    assert forwarded["seed"] == 1234
    assert forwarded["guided_json"] == {"type": "object"}


async def test_our_bearer_token_is_not_forwarded_upstream(
    client: httpx.AsyncClient, upstream: object
) -> None:
    """The harness key authenticates the desktop app to us, not us to the engine."""
    await client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": False}, headers=AUTH)
    headers = upstream.chat_calls[-1]["headers"]  # type: ignore[attr-defined]
    assert b"authorization" not in {key.lower() for key in headers}


async def test_the_upstream_is_addressed_by_model_ref_not_our_id(
    client: httpx.AsyncClient, upstream: object
) -> None:
    """The engine knows the model by its `model_ref`; the client knows it by our id.

    Ollama's `/v1/chat/completions` 404s on `glm-4.7-flash` and serves
    `glm-4.7-flash:q4_K_M`. Substituting in the proxy — rather than branching on
    the backend — is what keeps the catalog id stable across a backend migration.
    """
    from fake_upstream import DEFAULT_MODEL

    await client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": False}, headers=AUTH)
    forwarded = upstream.chat_calls[-1]["body"]  # type: ignore[attr-defined]
    assert forwarded["model"] == DEFAULT_MODEL
    assert forwarded["model"] != MODEL_ID
