"""FastAPI app factory and lifespan.

Two things here are load-bearing and easy to undo by accident:

* **No compression middleware.** `GZipMiddleware` anywhere in the stack turns the
  SSE stream into one blob at the end (`.claude/rules/streaming.md` §2). There is
  no middleware on this app at all, which is the safest version of that rule.
* **CORS off.** The desktop app is not a browser origin; enabling CORS would only
  widen the surface of a service that holds an API key.
"""

import contextlib
import logging
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI

from harness_control import __version__
from harness_control.catalog import Catalog, CatalogError, load_catalog
from harness_control.errors import register_error_handlers
from harness_control.logging_config import configure_logging
from harness_control.routes import admin, health, openai
from harness_control.settings import Settings, get_settings
from harness_control.supervisor.supervisor import Supervisor, UnknownModelError

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    supervisor: Supervisor | None = None,
    catalog: Catalog | None = None,
) -> FastAPI:
    """Build the app. Arguments exist for tests; production passes none of them."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.api_key)

    if supervisor is None:
        catalog = catalog or _load_catalog_or_die(settings)
        supervisor = Supervisor(catalog, settings)

    app = FastAPI(
        title="LLM Harness control plane",
        version=__version__,
        lifespan=_lifespan,
        # The OpenAPI schema is not part of the contract and the docs pages are a
        # needless surface on a service holding an API key.
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.supervisor = supervisor
    app.state.version = __version__
    app.state.started_at = time.monotonic()

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(openai.router)
    app.include_router(admin.router)
    return app


def _load_catalog_or_die(settings: Settings) -> Catalog:
    """An invalid catalog is a hard startup failure, never a partial catalog (L12)."""
    try:
        return load_catalog(settings.models_file)
    except CatalogError as exc:
        log.error("catalog is invalid: %s", exc)
        raise


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    supervisor: Supervisor = app.state.supervisor

    # One client for the whole process. `http2=False` (the default) because the
    # upstream is on localhost and h2 adds a framing layer that can coalesce
    # small SSE frames.
    app.state.http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    if settings.autoload_model:
        await _autoload(supervisor, settings.autoload_model)

    try:
        yield
    finally:
        await supervisor.shutdown()
        await app.state.http_client.aclose()


async def _autoload(supervisor: Supervisor, model_id: str) -> None:
    """Activate the configured model at startup.

    A failure here leaves the supervisor in `error` with `last_error` set and the
    control plane up: the client can then see *why* rather than finding a dead
    server. No crash-loop retry — ADR-0005.
    """
    log.info("autoloading %s", model_id)
    try:
        job = await supervisor.activate(model_id)
    except UnknownModelError:
        log.error("HARNESS_AUTOLOAD_MODEL=%s is not in the catalog", model_id)
        return
    if job.status == "failed":
        log.error("autoload of %s failed: %s", model_id, job.error)


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Build `harness_control.app:app` on first access, not at import.

    uvicorn and the systemd unit reference the module attribute `app`. Building it
    eagerly would read `models.yaml` and the environment merely to *import* this
    module, which every test does. PEP 562 lets the entrypoint stay the string the
    deploy artefacts already use.
    """
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _app
    if _app is None:
        _app = create_app()
    return _app
