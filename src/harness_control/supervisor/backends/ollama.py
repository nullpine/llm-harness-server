"""The `ollama` backend — the MVP path (`docs/BACKENDS.md` §2.1).

Ollama is a daemon that owns its own weights, so there is no process to spawn and
no directory to manage. "Activate" is a preload with `keep_alive: -1` (pin), and
"stop" is the same call with `keep_alive: 0` (unload now).
"""

from typing import Any

import httpx

from harness_control.catalog import ModelSpec
from harness_control.supervisor.backends.base import BackendError, ResourceInfo

#: Preloading an 18 GB model is not instant, and `/api/generate` does not return
#: until the weights are resident. It is still a single request, not a poll.
_ACTIVATE_TIMEOUT_S = 600.0
_QUICK_TIMEOUT_S = 5.0


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
        # model, or the next stop() would try again forever.
        self._model_ref = None
        await self._generate(model_ref, keep_alive=0, timeout_s=_QUICK_TIMEOUT_S)

    async def health(self) -> bool:
        """True when our tag appears in the daemon's loaded list."""
        if self._model_ref is None:
            return False
        try:
            response = await self._client.get(f"{self._url}/api/ps", timeout=_QUICK_TIMEOUT_S)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return self._model_ref in _loaded_names(payload)

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


def _loaded_names(payload: Any) -> set[str]:
    """The model tags in an `/api/ps` body, tolerating a daemon that answers oddly."""
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
