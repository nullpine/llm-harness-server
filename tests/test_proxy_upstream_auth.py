"""The relay must present the *upstream's* credentials, and only those.

Two keys are in play and they are not interchangeable:

* `HARNESS_API_KEY` authenticates the desktop app to us. It stops at `auth.py`.
* `HARNESS_REMOTE_API_KEY` authenticates *us* to a `remote_openai` upstream — a
  RunPod pod started with `VLLM_API_KEY` set, or a hosted provider.

Before `Backend.upstream_headers()` existed only the backend's own health probe
carried the second one, so against an authenticated upstream `/admin/state` said
`ready` while every chat completion 401'd. These tests pin both halves: the key
that must go, and the key that must not.

Unit coverage of the header itself lives in `test_backends.py`; this file asserts
it survives the whole request path.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from conftest import API_KEY, AUTH, REMOTE_API_KEY, activate_and_wait
from fake_upstream import DEFAULT_MODEL, FakeUpstream
from harness_control.app import create_app
from harness_control.catalog import Catalog, ModelSpec
from harness_control.settings import Settings
from harness_control.supervisor.supervisor import Supervisor

pytestmark = pytest.mark.asyncio

REMOTE_MODEL_ID = "remote-model"


@pytest.fixture
def remote_catalog() -> Catalog:
    """A one-entry catalog on the `remote_openai` backend.

    The shipped catalog is Ollama-only, and the header under test is empty there
    by design — so this path needs a catalog of its own.
    """
    return Catalog(
        [
            ModelSpec(
                id=REMOTE_MODEL_ID,
                display_name="Remote Model",
                backend="remote_openai",
                model_ref=DEFAULT_MODEL,
                estimated_load_seconds=0,
            )
        ]
    )


@pytest_asyncio.fixture
async def remote_client(
    remote_catalog: Catalog, settings: Settings, upstream: FakeUpstream
) -> AsyncIterator[httpx.AsyncClient]:
    supervisor = Supervisor(remote_catalog, settings)
    try:
        upstream.loaded.add(DEFAULT_MODEL)
        await activate_and_wait(supervisor, REMOTE_MODEL_ID)
        app: Any = create_app(settings, supervisor=supervisor)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://harness.test"
            ) as client:
                yield client
    finally:
        await supervisor.shutdown()


def _authorization(call: dict[str, Any]) -> str:
    """The `Authorization` the fake upstream actually received, as text."""
    raw: bytes = call["headers"].get(b"authorization", b"")
    return raw.decode()


async def test_the_relay_presents_the_upstream_key(
    remote_client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    response = await remote_client.post(
        "/v1/chat/completions",
        json={"model": REMOTE_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert upstream.chat_calls, "the request never reached the upstream"
    assert _authorization(upstream.chat_calls[-1]) == f"Bearer {REMOTE_API_KEY}"


async def test_the_relay_never_forwards_our_own_key(
    remote_client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    """Our key would be meaningless upstream, and handing it over leaks it."""
    await remote_client.post(
        "/v1/chat/completions",
        json={"model": REMOTE_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
        headers=AUTH,
    )
    sent = b" ".join(upstream.chat_calls[-1]["headers"].values()).decode()
    assert API_KEY not in sent


async def test_a_local_backend_sends_no_authorization_at_all(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    """`ollama` and `vllm` listen on 127.0.0.1 and authenticate nobody.

    Uses the default (Ollama) fixtures, so this is the MVP path.
    """
    await client.post(
        "/v1/chat/completions",
        json={"model": "glm-4.7-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers=AUTH,
    )
    assert upstream.chat_calls, "the request never reached the upstream"
    assert _authorization(upstream.chat_calls[-1]) == ""
