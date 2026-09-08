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
import os
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
        # `is None` for the same reason as the log buffers: Catalog defines
        # __len__, so an empty one is falsy and would be silently swapped for
        # whatever is on disk.
        if catalog is None:
            catalog = _load_catalog_or_die(settings)
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

    assert_single_model_daemon(settings)
    supervisor.start_watchdog()

    # SPEC §5.2: a reboot self-heals by restoring whatever was serving.
    model_id = settings.autoload_model or (
        supervisor.last_model() if settings.autoload_last else None
    )
    if model_id:
        await _autoload(supervisor, model_id)

    try:
        yield
    finally:
        await supervisor.shutdown()
        await app.state.http_client.aclose()


class MisconfiguredDaemonError(RuntimeError):
    """The Ollama daemon is configured in a way that breaks a core invariant."""


def assert_single_model_daemon(settings: Settings) -> None:
    """Refuse to start unless the daemon will hold exactly one model.

    `OLLAMA_MAX_LOADED_MODELS` defaults to 3. With it wrong, a switch leaves the
    old model resident alongside the new one while `/admin/state` reports one
    active — and on a machine with room to spare (this one has 48 GB) there is no
    memory pressure to reveal it. The invariant simply becomes quietly false.

    That invisibility is why this is fatal rather than a warning: a warning in a
    log nobody reads is indistinguishable from working, and the first symptom
    would be a user wondering why the wrong model answered.
    """
    if settings.default_backend != "ollama":
        return

    raw = os.environ.get("OLLAMA_MAX_LOADED_MODELS")
    if raw is None:
        raise MisconfiguredDaemonError(
            "OLLAMA_MAX_LOADED_MODELS is not set. Ollama defaults to 3 concurrent "
            "models, which silently breaks the single-active-model invariant: a "
            "switch would leave both models resident while /admin/state reports "
            "one. Start the daemon through scripts/dev-local.sh, or export "
            "OLLAMA_MAX_LOADED_MODELS=1 before starting it."
        )
    if raw.strip() != "1":
        raise MisconfiguredDaemonError(
            f"OLLAMA_MAX_LOADED_MODELS is {raw!r}, must be 1. Anything else lets "
            f"the daemon hold several models at once, which makes the active "
            f"model reported by /admin/state a guess rather than a fact."
        )


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
        log.error("%s is not in the catalog; nothing autoloaded", model_id)
        return
    if job is None:
        return
    # Activation is asynchronous now (contract §3), so startup does not block on
    # the load. The client watches /admin/state like any other switch.
    log.info("autoload of %s started as job %s", model_id, job.job_id)


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
