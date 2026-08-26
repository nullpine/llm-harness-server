"""`/admin/state`, and the proxy's state guards.

The guards live here rather than in `test_proxy_streaming.py` because they are
about the contract's state table, not about buffering — acceptance L6 and L7:
"never a hang or a 500".
"""

import asyncio

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


# --- L6: mid-switch requests are refused, never hung ---------------------------


async def test_l6_a_request_during_a_switch_is_503_with_retry_after(
    client: httpx.AsyncClient, ready_supervisor: Supervisor, upstream: object
) -> None:
    """The guard is load-bearing, not decoration.

    Ollama auto-loads on demand, so a completion arriving mid-switch would make
    the daemon load whatever model it names — two models resident, and the
    single-active invariant quietly false. Refusing is what prevents that.
    """
    upstream.generate_delay_s = 0.4  # type: ignore[attr-defined]
    await ready_supervisor.activate("qwen3.8-27b")

    saw_503 = False
    for _ in range(60):
        response = await client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH)
        if response.status_code == 503:
            saw_503 = True
            assert response.json()["error"]["code"] == "model_loading"
            # The incoming model's advertised time, not a fallback guess.
            assert int(response.headers["Retry-After"]) == 12
            break
        await asyncio.sleep(0.01)

    await ready_supervisor.wait_for_activation()
    assert saw_503, "no request was refused during the switch"


async def test_l6_the_daemon_is_never_asked_to_generate_mid_switch(
    client: httpx.AsyncClient, ready_supervisor: Supervisor, upstream: object
) -> None:
    """The refusal happens before the proxy reaches upstream, not after."""
    upstream.generate_delay_s = 0.3  # type: ignore[attr-defined]
    await ready_supervisor.activate("qwen3.8-27b")

    before = len(upstream.chat_calls)  # type: ignore[attr-defined]
    for _ in range(20):
        await client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH)
        if ready_supervisor.state is ModelState.READY:
            break
        await asyncio.sleep(0.01)
    await ready_supervisor.wait_for_activation()

    refused_while_switching = len(upstream.chat_calls) - before  # type: ignore[attr-defined]
    assert refused_while_switching == 0, "a completion reached the daemon mid-switch"


# --- the admin surface --------------------------------------------------------


async def test_activate_returns_202_with_a_job(
    client: httpx.AsyncClient, ready_supervisor: Supervisor
) -> None:
    response = await client.post("/admin/models/qwen3.8-27b/activate", headers=AUTH)

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"].startswith("act_")
    assert body["model_id"] == "qwen3.8-27b"
    assert body["estimated_seconds"] == 12
    await ready_supervisor.wait_for_activation()


async def test_activating_the_active_model_returns_200(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/models/glm-4.7-flash/activate", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "job_id": None,
        "model_id": "glm-4.7-flash",
        "already_active": True,
    }


async def test_activating_an_unknown_model_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/models/no-such-model/activate", headers=AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_l10_a_second_activation_is_409(
    client: httpx.AsyncClient, ready_supervisor: Supervisor, upstream: object
) -> None:
    upstream.generate_delay_s = 0.3  # type: ignore[attr-defined]

    first = await client.post("/admin/models/qwen3.8-27b/activate", headers=AUTH)
    second = await client.post("/admin/models/glm-4.7-flash/activate", headers=AUTH)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "activation_in_progress"
    await ready_supervisor.wait_for_activation()


async def test_admin_models_uses_the_v1_1_field_names(client: httpx.AsyncClient) -> None:
    """`model_ref` and `available`, not `hf_repo`/`downloaded` — renamed in v1.1."""
    response = await client.get("/admin/models", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"active_model_id", "state", "models"}

    row = next(m for m in body["models"] if m["id"] == "glm-4.7-flash")
    assert row["model_ref"] == "glm-4.7-flash:q4_K_M"
    assert row["available"] is True
    assert "hf_repo" not in row
    assert "downloaded" not in row


async def test_admin_models_reports_availability_from_tags_not_residency(
    client: httpx.AsyncClient, upstream: object
) -> None:
    """A pulled-but-not-loaded model is available; otherwise nothing is switchable."""
    body = (await client.get("/admin/models", headers=AUTH)).json()
    inactive = next(m for m in body["models"] if m["id"] == "qwen3.8-27b")

    assert inactive["state"] == "idle", "only the active model carries live state"
    assert inactive["available"] is True, "not loaded is not the same as not available"


async def test_admin_models_marks_an_unpulled_model_unavailable(
    client: httpx.AsyncClient, upstream: object
) -> None:
    upstream.pulled = {"glm-4.7-flash:q4_K_M"}  # type: ignore[attr-defined]

    body = (await client.get("/admin/models", headers=AUTH)).json()
    row = next(m for m in body["models"] if m["id"] == "qwen3.8-27b")

    assert row["available"] is False


async def test_admin_jobs_reports_a_job(
    client: httpx.AsyncClient, ready_supervisor: Supervisor
) -> None:
    started = await client.post("/admin/models/qwen3.8-27b/activate", headers=AUTH)
    job_id = started.json()["job_id"]
    await ready_supervisor.wait_for_activation()

    response = await client.get(f"/admin/jobs/{job_id}", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["status"] == "succeeded"
    assert body["model_id"] == "qwen3.8-27b"
    assert body["finished_at"] is not None


async def test_admin_jobs_404s_for_an_unknown_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/jobs/act_99999999", headers=AUTH)
    assert response.status_code == 404


async def test_admin_logs_returns_the_ring_buffer(
    client: httpx.AsyncClient, ready_supervisor: Supervisor
) -> None:
    ready_supervisor.logbuf.append("INFO something happened")

    response = await client.get("/admin/logs?lines=50&source=control", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "control"
    assert "INFO something happened" in body["lines"]


async def test_admin_logs_rejects_an_unknown_source(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/logs?source=syslog", headers=AUTH)
    assert response.status_code == 422
