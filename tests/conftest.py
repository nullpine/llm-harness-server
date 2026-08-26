"""Shared fixtures.

Two ways to reach the app, and the difference matters:

* `client` — httpx over `ASGITransport`. Fast, in-process, and **it buffers**:
  the whole response body arrives at once regardless of what the app does. Fine
  for status codes and JSON bodies, useless for streaming.
* `live_app` — the app on a real uvicorn socket. Slower, but it is the only way
  to observe whether frames actually arrive incrementally, which is the entire
  point of `test_proxy_streaming.py`.

The fake upstream is likewise served over a real socket, so the relay talks real
HTTP to it rather than a transport that could smuggle frames through in one go.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn

from fake_upstream import DEFAULT_MODEL, FakeUpstream
from harness_control.app import create_app
from harness_control.catalog import Catalog, ModelSpec, load_catalog
from harness_control.settings import Settings
from harness_control.supervisor.backends import Backend, OllamaBackend
from harness_control.supervisor.supervisor import Supervisor

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CATALOG = REPO_ROOT / "deploy" / "config" / "models.yaml"

API_KEY = "test-key-0123456789abcdefghijklmnop"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
MODEL_ID = "glm-4.7-flash"


class LiveServer:
    """A uvicorn server on an ephemeral port, for tests that need real sockets."""

    def __init__(self, url: str) -> None:
        self.url = url


@contextlib.asynccontextmanager
async def serve(app: Any, host: str = "127.0.0.1") -> AsyncIterator[LiveServer]:
    config = uvicorn.Config(app, host=host, port=0, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():  # pragma: no cover - the server failed to bind
                await task
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield LiveServer(f"http://{host}:{port}")
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=10)


# --------------------------------------------------------------- the upstream


@pytest.fixture
def upstream() -> FakeUpstream:
    """The fake engine. Mutate its fields to shape what the next request does."""
    return FakeUpstream()


@pytest_asyncio.fixture
async def upstream_server(upstream: FakeUpstream) -> AsyncIterator[LiveServer]:
    async with serve(upstream) as server:
        yield server


# ----------------------------------------------------------------- the catalog


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog(SHIPPED_CATALOG)


@pytest.fixture
def spec(catalog: Catalog) -> ModelSpec:
    found = catalog.get(MODEL_ID)
    assert found is not None
    return found


# ---------------------------------------------------------------- the settings


@pytest.fixture
def settings(upstream_server: LiveServer, tmp_path: Path) -> Settings:
    """Everything pointed at the fake upstream and a scratch state dir.

    Nothing here reads the real environment: `Settings` is constructed explicitly
    so a developer's exported `HARNESS_*` cannot change what the suite asserts.
    """
    return Settings(
        api_key=API_KEY,
        models_file=SHIPPED_CATALOG,
        default_backend="ollama",
        ollama_url=upstream_server.url,
        remote_base_url=upstream_server.url,
        state_dir=tmp_path,
        load_timeout_s=10.0,
        health_poll_interval_s=0.02,
        autoload_model="",
    )


@pytest.fixture
def backend_factory(
    upstream_server: LiveServer,
) -> Callable[[str, Settings], Backend]:
    """Every backend built for a test points at the fake upstream."""

    def build(name: str, settings: Settings) -> Backend:
        from harness_control.supervisor.backends import create_backend

        if name == "vllm":
            # The vllm backend spawns a process; tests that want that build it
            # themselves (test_backends.py). Anything else gets the ollama path.
            raise NotImplementedError("vllm is built explicitly in tests that need it")
        return create_backend(name, settings)

    return build


# -------------------------------------------------------------- the supervisor


@pytest_asyncio.fixture
async def supervisor(
    catalog: Catalog,
    settings: Settings,
    backend_factory: Callable[[str, Settings], Backend],
) -> AsyncIterator[Supervisor]:
    sup = Supervisor(catalog, settings, backend_factory=backend_factory)
    try:
        yield sup
    finally:
        await sup.shutdown()


@pytest_asyncio.fixture
async def ready_supervisor(
    supervisor: Supervisor, upstream: FakeUpstream
) -> AsyncIterator[Supervisor]:
    """A supervisor with the MVP model actually loaded and `ready`."""
    upstream.loaded.add(DEFAULT_MODEL)
    await supervisor.activate(MODEL_ID)
    yield supervisor


# --------------------------------------------------------------------- the app


@pytest_asyncio.fixture
async def app(settings: Settings, ready_supervisor: Supervisor) -> AsyncIterator[Any]:
    fastapi_app = create_app(settings, supervisor=ready_supervisor)
    # Enter the lifespan by hand so app.state.http_client exists without going
    # through a server; the live_app fixture runs the real one.
    async with httpx.AsyncClient() as http_client:
        fastapi_app.state.http_client = http_client
        yield fastapi_app


@pytest_asyncio.fixture
async def idle_app(settings: Settings, supervisor: Supervisor) -> AsyncIterator[Any]:
    """The app with nothing loaded — `state == idle`."""
    fastapi_app = create_app(settings, supervisor=supervisor)
    async with httpx.AsyncClient() as http_client:
        fastapi_app.state.http_client = http_client
        yield fastapi_app


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """In-process, buffering. Do not use for streaming assertions."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness.test") as c:
        yield c


@pytest_asyncio.fixture
async def idle_client(idle_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=idle_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness.test") as c:
        yield c


@pytest_asyncio.fixture
async def live_app(settings: Settings, ready_supervisor: Supervisor) -> AsyncIterator[LiveServer]:
    """The real thing on a real socket: the only fixture that can see buffering."""
    fastapi_app = create_app(settings, supervisor=ready_supervisor)
    async with serve(fastapi_app) as server:
        yield server


@pytest.fixture
def ollama_backend(upstream_server: LiveServer) -> OllamaBackend:
    return OllamaBackend(upstream_server.url)
