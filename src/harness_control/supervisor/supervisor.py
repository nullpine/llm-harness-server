"""The supervisor: the state machine, the activation lock, and the job registry.

It owns *when* things happen. A backend owns *how* — see
`.claude/rules/backend-boundary.md`. Nothing here branches on which backend it
holds, and no backend here is asked to decide state.

Everything hard about switching lives in the **transition**, not in either steady
state:

  stopping -> drain in-flight -> stop -> await release -> loading -> ready

Two properties of the Ollama backend shape the design, both found the hard way
during M1 verification:

  * `stop()` does not make a model unreachable. `keep_alive: 0` unloads the
    weights, and the very next request transparently reloads them. What enforces
    the single-active invariant is the **proxy state guard**, not the unload.
  * The daemon auto-loads on demand, so a request arriving mid-switch would load
    whatever model it names unless the guard refuses it first. The guard is
    load-bearing, not defensive decoration.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable

from harness_control.catalog import Catalog, ModelSpec
from harness_control.logbuf import LogBuffer
from harness_control.models import GpuInfo, StateResponse
from harness_control.settings import Settings
from harness_control.supervisor.backends import Backend, BackendError, create_backend
from harness_control.supervisor.jobs import Job, JobRegistry, utcnow_iso
from harness_control.supervisor.readiness import poll_until
from harness_control.supervisor.state import ModelState, check_transition

log = logging.getLogger(__name__)

#: How often the watchdog asks whether the backend is still serving. L8 allows
#: 5 s to notice a death, so the interval has to leave room for the health call.
WATCHDOG_INTERVAL_S = 2.0

#: How long to wait for the old model to actually leave memory (Part 3).
UNLOAD_TIMEOUT_S = 30.0

#: How often the drain checks whether in-flight completions have finished.
DRAIN_POLL_INTERVAL_S = 0.1


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
        logbuf: LogBuffer | None = None,
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
        #: Completions currently being proxied. The proxy increments on entry and
        #: decrements in a `finally`, so the drain can wait for real work rather
        #: than for a fixed delay.
        self._in_flight = 0
        self._activation: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._progress_hint: str | None = None
        #: What a switch is loading, for the Retry-After estimate.
        self._pending_model_id: str | None = None
        self._logbuf = logbuf or LogBuffer()

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

    @property
    def pending_model_id(self) -> str | None:
        """The model a switch is loading, if one is under way.

        Mid-switch there is no *active* model, so a `Retry-After` derived from
        the active one falls back to a guess. The incoming model's advertised
        load time is the honest estimate, and it is the number the desktop app
        already shows in its confirmation dialog.
        """
        return self._pending_model_id

    async def is_available(self, spec: ModelSpec) -> bool:
        """Whether a catalogued model can be served without a fetch.

        Asked of a backend built for that model's own `backend:` — a mixed
        catalog is explicitly supported (`docs/BACKENDS.md` §4.3), so the active
        backend is the wrong thing to ask about a model it does not serve.
        """
        if self._backend is not None and spec.id == self._active_model_id:
            return await self._backend.is_available(spec)
        probe = self._make_backend(spec)
        try:
            return await probe.is_available(spec)
        except BackendError:
            return False
        finally:
            await probe.aclose()

    async def state_payload(self) -> StateResponse:
        """The body of `GET /admin/state`. Cheap enough to poll every 2 s."""
        progress: str | None = self._progress_hint
        gpus: list[GpuInfo] = []
        if self._backend is not None:
            try:
                # A backend that can say something more specific — a download
                # percentage, say — wins over the supervisor's phase label.
                progress = await self._backend.progress_hint() or progress
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

    def track_request(self) -> "_InFlight":
        """Context manager the proxy wraps each completion in, so the drain can
        wait for real work rather than for a fixed delay."""
        return _InFlight(self)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def activate(self, model_id: str) -> Job | None:
        """Start a switch and return its job, or None if it is already active.

        Returns as soon as the job exists — the contract says activation is
        asynchronous and the client polls `/admin/state` (§3). Blocking here
        would make the 202 a lie and hold the request open for a minute.
        """
        spec = self._catalog.get(model_id)
        if spec is None:
            raise UnknownModelError(f"no model {model_id!r} in the catalog")

        # The lock is checked *before* "is it already active", and the order
        # matters. `activate()` returns before its task runs, so mid-switch the
        # state still reads as the outgoing model being ready — and asking for
        # that model would be answered "already active" while it is in fact being
        # torn down. Non-blocking either way: a second activation is told so
        # (L10), not queued behind the first with no way to know.
        if self._lock.locked():
            raise ActivationInProgressError("an activation is already in flight")

        if self._active_model_id == model_id and self._state is ModelState.READY:
            return None

        await self._lock.acquire()

        job = self._jobs.create(model_id)
        job.start()

        # `stopping` is entered here, synchronously, rather than inside the task.
        # SPEC §5.2 step 1 is "set state = stopping; stop accepting new /v1
        # requests", and the 202 should already mean that. Leaving it to the task
        # opens a window — however short — in which the client has been told the
        # switch is under way while completions still reach the old model.
        self._transition(ModelState.STOPPING)
        self._progress_hint = "finishing in-flight replies"
        self._pending_model_id = model_id

        self._activation = asyncio.create_task(self._activation_task(spec, job))
        return job

    async def wait_for_activation(self) -> None:
        """Await the running activation, if any.

        Exists because `activate()` deliberately returns before its work starts:
        anything wanting to know the outcome has to wait on the task, not poll
        the state, which is still the *old* state until the task first runs.
        """
        task = self._activation
        if task is not None:
            await asyncio.shield(asyncio.wait_for(asyncio.shield(task), timeout=None))

    async def _activation_task(self, spec: ModelSpec, job: Job) -> None:
        """The transition, start to finish. Owns the lock for its whole life."""
        try:
            await self._run_activation(spec)
        except (BackendError, TimeoutError) as exc:
            self._fail(str(exc))
            job.fail(str(exc), self._log_tail())
            log.error("activation of %s failed: %s", spec.id, exc)
        except Exception as exc:  # a bug, not a backend failure — still not fatal
            self._fail(f"unexpected error: {exc}")
            job.fail(str(exc), self._log_tail())
            log.exception("unexpected failure activating %s", spec.id)
        else:
            job.succeed()
        finally:
            self._lock.release()

    async def _run_activation(self, spec: ModelSpec) -> None:
        """stopping -> drain -> stop -> await release -> loading -> ready.

        Entered already in `stopping`: `activate()` makes that transition before
        returning, so no request can slip through between the 202 and the first
        tick of this task.
        """
        # New requests are already refused — the proxy's guard keys off state.
        # In-flight ones get to finish.
        await self._drain()

        self._progress_hint = "unloading the previous model"
        await self._stop_current()

        backend = self._make_backend(spec)
        self._backend = backend
        self._backend_name = spec.backend
        self._transition(ModelState.LOADING)
        self._progress_hint = "loading weights"

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

        self._active_model_id = spec.id
        self._last_error = None
        self._progress_hint = None
        self._pending_model_id = None
        self._transition(ModelState.READY)
        self._persist_last_model(spec.id)
        log.info("model %s is ready on the %s backend", spec.id, spec.backend)

    async def _drain(self) -> None:
        """Let in-flight completions finish, up to `HARNESS_DRAIN_TIMEOUT_S`.

        A stream that is still producing tokens for a reader deserves to finish;
        one that has hung does not get to block the switch forever.
        """
        if self._in_flight == 0:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.drain_timeout_s
        log.info("draining %d in-flight completion(s)", self._in_flight)

        while self._in_flight > 0:
            if loop.time() >= deadline:
                log.warning(
                    "drain timed out with %d completion(s) still in flight; switching anyway",
                    self._in_flight,
                )
                return
            await asyncio.sleep(DRAIN_POLL_INTERVAL_S)

    async def _stop_current(self) -> None:
        """Unload the current model, wait for it to actually go, release the backend."""
        backend, self._backend = self._backend, None
        self._backend_name = None
        if backend is None:
            return
        try:
            await backend.stop()
            # Part 3. Not memory safety on this machine — both models fit — but
            # correctness: loading on top of an incomplete unload leaves two
            # models resident while we report one active, and nothing else in the
            # system would notice.
            await backend.await_released(UNLOAD_TIMEOUT_S)
        finally:
            # Even a backend that failed to unload must not leak its connections;
            # the supervisor owns lifecycle, the backend owns behaviour.
            await backend.aclose()
        self._previous_model_id = self._active_model_id
        self._active_model_id = None

    async def shutdown(self) -> None:
        """Release everything on control-plane shutdown. Never raises."""
        await self.stop_watchdog()

        task, self._activation = self._activation, None
        if task is not None and not task.done():
            # A switch in progress is abandoned rather than awaited: the process
            # is going away, and the next start restores from `last_model`.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        try:
            await self._stop_current()
        except BackendError:
            log.warning("backend did not stop cleanly during shutdown", exc_info=True)
        self._backend = None
        self._backend_name = None

    # ------------------------------------------------------------- watchdog

    def start_watchdog(self) -> None:
        """Notice the backend dying under us within L8's 5 s.

        No automatic restart: a crashed model stays in `error` until a client
        activates something. Auto-restart hides real failures — ADR-0005.
        """
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def stop_watchdog(self) -> None:
        task, self._watchdog = self._watchdog, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            # Only `ready` is watched. During a switch the supervisor is already
            # driving the backend and a transient unhealthy answer is expected.
            if self._state is not ModelState.READY or self._backend is None:
                continue
            try:
                healthy = await self._backend.health()
            except Exception:  # the health check itself failing is a death too
                healthy = False
            if not healthy:
                log.error("backend for %s stopped answering", self._active_model_id)
                self._fail("the model backend stopped responding")

    def _persist_last_model(self, model_id: str) -> None:
        """Remember what was serving, so a restart can restore it (SPEC §5.2)."""
        try:
            self._settings.state_dir.mkdir(parents=True, exist_ok=True)
            (self._settings.state_dir / "last_model").write_text(model_id, encoding="utf-8")
        except OSError as exc:
            # Losing this costs a reboot's convenience, never a request.
            log.warning("could not persist last_model: %s", exc)

    def last_model(self) -> str | None:
        try:
            return (self._settings.state_dir / "last_model").read_text(encoding="utf-8").strip()
        except OSError:
            return None

    @property
    def logbuf(self) -> LogBuffer:
        return self._logbuf

    def _log_tail(self) -> list[str]:
        return self._logbuf.tail(20)

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


class _InFlight:
    """Counts a completion for the duration of its stream.

    The proxy wraps every relay in one of these, so `_drain()` waits for actual
    work to finish rather than for a fixed delay. Decrementing in `__aexit__`
    means an aborted or failed stream releases the drain just as a completed one
    does — otherwise a client hanging up mid-switch would stall it for the full
    timeout.
    """

    def __init__(self, supervisor: Supervisor) -> None:
        self._supervisor = supervisor

    async def __aenter__(self) -> None:
        self._supervisor._in_flight += 1

    async def __aexit__(self, *_exc: object) -> None:
        self._supervisor._in_flight -= 1
