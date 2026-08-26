"""The supervisor: the state machine, the activation lock, and the job registry.

It owns *when* things happen. A backend owns *how* — see
`.claude/rules/backend-boundary.md`. Nothing here branches on which backend it
holds, and no backend here is asked to decide state.

M1 implements `activate()` and `state()`. The drain and the watchdog are M2
(`docs/BACKLOG.md`); the state machine already has the edges they will use.
"""

import asyncio
import logging
from collections.abc import Callable

from harness_control.catalog import Catalog, ModelSpec
from harness_control.models import GpuInfo, StateResponse
from harness_control.settings import Settings
from harness_control.supervisor.backends import (
    Backend,
    BackendError,
    close_backend,
    create_backend,
)
from harness_control.supervisor.jobs import Job, JobRegistry, utcnow_iso
from harness_control.supervisor.readiness import poll_until
from harness_control.supervisor.state import ModelState, check_transition

log = logging.getLogger(__name__)


class ActivationInProgressError(Exception):
    """Another activation holds the lock. The route turns this into a 409."""


class UnknownModelError(Exception):
    """No such id in the catalog. The route turns this into a 404."""


class Supervisor:
    """One model active at a time, across every backend in the catalog."""

    def __init__(
        self,
        catalog: Catalog,
        settings: Settings,
        *,
        backend_factory: Callable[[str, Settings], Backend] | None = None,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        self._state = ModelState.IDLE
        self._active_model_id: str | None = None
        self._previous_model_id: str | None = None
        self._since = utcnow_iso()
        self._last_error: str | None = None
        self._lock = asyncio.Lock()
        self._jobs = JobRegistry()
        self._backend: Backend | None = None
        self._backend_name: str | None = None
        # Injected only by tests, which need backends pointed at a fake upstream.
        self._factory = backend_factory

    # ------------------------------------------------------------- reading

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def state(self) -> ModelState:
        return self._state

    @property
    def active_model_id(self) -> str | None:
        return self._active_model_id

    @property
    def jobs(self) -> JobRegistry:
        return self._jobs

    @property
    def backend(self) -> Backend | None:
        """The active backend, or None. `proxy.py` reads `base_url` off this and
        nothing else about it."""
        return self._backend

    def base_url(self) -> str | None:
        return self._backend.base_url if self._backend is not None else None

    @property
    def active_model_ref(self) -> str | None:
        """What the *upstream* calls the active model, as opposed to our catalog id.

        `docs/BACKENDS.md` §3: `model_ref` is "an Ollama tag, a Hugging Face repo,
        or a provider's model string" — i.e. by definition the name the engine
        answers to. The proxy substitutes it into the outgoing body, which is why
        no backend passes `--served-model-name` to rename itself back.
        """
        if self._active_model_id is None:
            return None
        spec = self._catalog.get(self._active_model_id)
        return spec.model_ref if spec else None

    def estimated_seconds(self, model_id: str) -> int:
        spec = self._catalog.get(model_id)
        return spec.estimated_load_seconds if spec else 0

    async def state_payload(self) -> StateResponse:
        """The body of `GET /admin/state`. Cheap enough to poll every 2 s."""
        progress: str | None = None
        gpus: list[GpuInfo] = []
        if self._backend is not None:
            try:
                progress = await self._backend.progress_hint()
                gpus = [GpuInfo(**info) for info in await self._backend.resources()]
            except Exception:  # /admin/state must never fail, whatever the backend does
                log.warning("backend could not report progress or resources", exc_info=True)
        return StateResponse(
            state=self._state,
            active_model_id=self._active_model_id,
            previous_model_id=self._previous_model_id,
            since=self._since,
            progress_hint=progress,
            last_error=self._last_error,
            gpu=gpus,
        )

    # ------------------------------------------------------------ mutating

    async def activate(self, model_id: str) -> Job:
        """Make `model_id` the serving model, synchronously.

        M1 loads one model at startup, so this blocks until the model is ready or
        the load fails. `POST /admin/models/{id}/activate` — which must return 202
        immediately and run this in the background — is M2.
        """
        spec = self._catalog.get(model_id)
        if spec is None:
            raise UnknownModelError(f"no model {model_id!r} in the catalog")

        if self._lock.locked():
            raise ActivationInProgressError("an activation is already in flight")

        async with self._lock:
            job = self._jobs.create(model_id)
            job.start()
            try:
                await self._run_activation(spec)
            except (BackendError, TimeoutError) as exc:
                self._fail(str(exc))
                job.fail(str(exc))
                log.error("activation of %s failed: %s", model_id, exc)
            else:
                job.succeed()
            return job

    async def _run_activation(self, spec: ModelSpec) -> None:
        """stopping → loading → ready, or → error. The only path that sets state."""
        self._transition(ModelState.STOPPING)
        await self._stop_current()

        backend = self._make_backend(spec)
        self._backend = backend
        self._backend_name = spec.backend
        self._transition(ModelState.LOADING)

        await backend.activate(spec)

        ready = await poll_until(
            backend.health,
            timeout_s=self._settings.load_timeout_s,
            interval_s=self._settings.health_poll_interval_s,
        )
        if not ready:
            raise TimeoutError(
                f"{spec.id} was not ready after {self._settings.load_timeout_s:.0f}s "
                f"(backend {spec.backend})"
            )

        self._previous_model_id = self._active_model_id
        self._active_model_id = spec.id
        self._last_error = None
        self._transition(ModelState.READY)
        log.info("model %s is ready on the %s backend", spec.id, spec.backend)

    async def shutdown(self) -> None:
        """Release the backend on control-plane shutdown. Never raises."""
        try:
            await self._stop_current()
        except BackendError:
            log.warning("backend did not stop cleanly during shutdown", exc_info=True)
        self._backend = None
        self._backend_name = None

    async def _stop_current(self) -> None:
        """Unload the current model and release its transport. Safe when idle."""
        backend, self._backend = self._backend, None
        self._backend_name = None
        if backend is None:
            return
        try:
            await backend.stop()
        finally:
            # Even a backend that failed to unload must not leak its connections;
            # the supervisor owns lifecycle, the backend owns behaviour.
            await close_backend(backend)
        self._previous_model_id = self._active_model_id
        self._active_model_id = None

    def _make_backend(self, spec: ModelSpec) -> Backend:
        """One backend instance per activation, so a switch cannot inherit state."""
        if self._factory is not None:
            return self._factory(spec.backend, self._settings)
        return create_backend(spec.backend, self._settings)

    def _transition(self, target: ModelState) -> None:
        check_transition(self._state, target)
        log.debug("state %s -> %s", self._state.value, target.value)
        self._state = target
        self._since = utcnow_iso()

    def _fail(self, message: str) -> None:
        self._last_error = message
        self._active_model_id = None
        if self._state is not ModelState.ERROR:
            self._transition(ModelState.ERROR)
