"""The `ollama` backend — the MVP path (`docs/BACKENDS.md` §2.1).

Ollama is a daemon that owns its own weights, so there is no process to spawn and
no directory to manage. "Activate" is a preload with `keep_alive: -1` (pin), and
"stop" is the same call with `keep_alive: 0` (unload now).
"""

import asyncio
import logging
import os
from typing import Any

import httpx

from harness_control.catalog import ModelSpec
from harness_control.supervisor.backends.base import BackendError, ResourceInfo

log = logging.getLogger(__name__)

#: Preloading an 18 GB model is not instant, and `/api/generate` does not return
#: until the weights are resident. It is still a single request, not a poll.
_ACTIVATE_TIMEOUT_S = 600.0
_QUICK_TIMEOUT_S = 5.0
_UNLOAD_POLL_INTERVAL_S = 0.25


class OllamaBackend:
    """Talks to the Ollama daemon over its native `/api/*` routes.

    OpenAI traffic does not come through here at all — the proxy takes `base_url`
    and appends `/v1/chat/completions` itself.
    """

    name = "ollama"

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_QUICK_TIMEOUT_S)
        self._owns_client = client is None
        self._model_ref: str | None = None
        #: What `stop()` last asked the daemon to unload, for `await_released()`.
        self._released_ref: str | None = None

    @property
    def base_url(self) -> str:
        return self._url

    @property
    def model_ref(self) -> str | None:
        """The tag currently pinned, or None. Read by tests, not by the supervisor."""
        return self._model_ref

    async def activate(self, spec: ModelSpec) -> None:
        """Preload and pin `spec.model_ref`.

        `keep_alive: -1` means "never unload on a timer": the supervisor decides
        when a model goes away, not Ollama's idle sweeper.
        """
        await self._generate(spec.model_ref, keep_alive=-1, timeout_s=_ACTIVATE_TIMEOUT_S)
        self._model_ref = spec.model_ref

    async def stop(self) -> None:
        """Unload the pinned model. Idempotent: nothing pinned is a no-op."""
        model_ref = self._model_ref
        if model_ref is None:
            return
        # Clear first: a failed unload must not leave us believing we still own a
        # model, or the next stop() would try again forever. Remembered for
        # `await_released()`, which has to know what it is waiting to disappear.
        self._model_ref = None
        self._released_ref = model_ref
        await self._generate(model_ref, keep_alive=0, timeout_s=_QUICK_TIMEOUT_S)

    async def health(self) -> bool:
        """True when our tag appears in the daemon's loaded list."""
        if self._model_ref is None:
            return False
        try:
            return self._model_ref in await self.loaded_models()
        except BackendError:
            return False

    async def loaded_models(self) -> set[str]:
        """Every tag the daemon currently holds in memory (`GET /api/ps`).

        The supervisor asks "is the old model gone yet"; knowing that `/api/ps`
        is the place to look — and that it is not the same question as "is the
        tag downloaded" — is the backend's business
        (`.claude/rules/backend-boundary.md`).
        """
        return await self._names_from("/api/ps")

    async def available_models(self) -> set[str]:
        """Every tag pulled to disk (`GET /api/tags`).

        Deliberately not `/api/ps`: that lists what is *loaded right now*, so
        using it for `available` in `/admin/models` would report a perfectly
        usable model as unavailable the moment it is not the active one.
        """
        return await self._names_from("/api/tags")

    async def _names_from(self, path: str) -> set[str]:
        try:
            response = await self._client.get(f"{self._url}{path}", timeout=_QUICK_TIMEOUT_S)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendError(f"ollama at {self._url}{path} did not answer: {exc}") from exc
        return _model_names(payload)

    async def max_loaded_models(self) -> int | None:
        """What the daemon will hold at once, or None if it will not say.

        `OLLAMA_MAX_LOADED_MODELS` defaults to 3. This machine has 48 GB, so both
        models fit at once: a daemon started without it does not thrash, it
        quietly keeps the old model resident while the supervisor reports one
        active. That is why this is asserted at startup rather than assumed —
        the failure is invisible, not loud. See `docs/BACKENDS.md` §2.1.
        """
        try:
            response = await self._client.get(f"{self._url}/api/version", timeout=_QUICK_TIMEOUT_S)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        # Ollama exposes no config endpoint, so this is read from the environment
        # the control plane and the daemon are expected to share (dev-local.sh
        # starts both). None means "cannot tell", which is not the same as wrong.
        raw = os.environ.get("OLLAMA_MAX_LOADED_MODELS")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    async def await_released(self, timeout_s: float) -> None:
        """Poll `/api/ps` until the tag we just unloaded is gone.

        A correctness guard, not a memory-safety one: this machine has room for
        both models, so an unload that never completed would not thrash. It would
        simply leave two models resident while the supervisor reports one active
        — true state and reported state diverging, with nothing to notice it.

        `stop()` returning is not enough on its own: Ollama's unload is
        asynchronous, and `keep_alive: 0` only requests it.
        """
        if self._released_ref is None:
            return

        released = self._released_ref
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + timeout_s
        polls = 0

        while True:
            polls += 1
            try:
                loaded = await self.loaded_models()
            except BackendError as exc:
                # The daemon is unreachable, so nothing of ours is resident.
                log.info("unload wait: daemon unreachable (%s); treating as released", exc)
                self._released_ref = None
                return

            elapsed_ms = int((loop.time() - started) * 1000)
            if released not in loaded:
                log.info(
                    "unload wait: %r gone after %d poll(s), %d ms",
                    released,
                    polls,
                    elapsed_ms,
                )
                self._released_ref = None
                return

            # Every poll, because this is the wait that silently leaves two
            # models resident if it is ever wrong.
            log.debug(
                "unload wait: /api/ps still lists %r after %d ms (loaded=%s)",
                released,
                elapsed_ms,
                sorted(loaded),
            )
            if loop.time() >= deadline:
                raise BackendError(
                    f"{released!r} was still loaded {timeout_s:.0f}s after being unloaded "
                    f"({polls} polls of /api/ps, last saw {sorted(loaded)}); "
                    f"refusing to activate another model on top of it"
                )
            await asyncio.sleep(_UNLOAD_POLL_INTERVAL_S)

    async def is_available(self, spec: ModelSpec) -> bool:
        """Whether the tag is pulled. `/api/tags`, not `/api/ps` — see above."""
        try:
            return spec.model_ref in await self.available_models()
        except BackendError:
            return False

    async def progress_hint(self) -> str | None:
        """None in M1. Parsing pull progress is an M2 item (docs/BACKLOG.md)."""
        return None

    async def resources(self) -> list[ResourceInfo]:
        """Empty: no `nvidia-smi` on Apple Silicon, and unified memory is not worth
        modelling in the MVP (`docs/BACKENDS.md` §2.1). L11 covers the empty case."""
        return []

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _generate(self, model_ref: str, *, keep_alive: int, timeout_s: float) -> None:
        """`POST /api/generate` with no prompt: load or unload, depending on keep_alive."""
        try:
            response = await self._client.post(
                f"{self._url}/api/generate",
                json={"model": model_ref, "keep_alive": keep_alive},
                timeout=timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BackendError(
                f"ollama refused keep_alive={keep_alive} for {model_ref!r}: "
                f"HTTP {exc.response.status_code} {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"ollama at {self._url} is unreachable: {exc}") from exc


def _model_names(payload: Any) -> set[str]:
    """Tags out of an `/api/ps` or `/api/tags` body, tolerating an odd answer."""
    if not isinstance(payload, dict):
        return set()
    models = payload.get("models")
    if not isinstance(models, list):
        return set()
    names: set[str] = set()
    for entry in models:
        if isinstance(entry, dict):
            for key in ("name", "model"):
                value = entry.get(key)
                if isinstance(value, str):
                    names.add(value)
    return names
