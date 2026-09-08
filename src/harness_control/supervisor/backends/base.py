"""The Backend Protocol — the contract between the supervisor and every engine.

Written out in `docs/BACKENDS.md` §1; the signatures here must match it exactly.
A backend owns only *how a model is made to serve*. It never sets state, never
decides policy, and never knows about the state machine — see
`.claude/rules/backend-boundary.md`.
"""

from collections.abc import Mapping
from typing import Protocol, TypedDict, runtime_checkable

from harness_control.catalog import ModelSpec


class ResourceInfo(TypedDict):
    index: int
    name: str
    memory_used_mb: int
    memory_total_mb: int
    utilization_pct: int


@runtime_checkable
class Backend(Protocol):
    name: str  # "ollama" | "vllm" | "remote_openai"

    @property
    def base_url(self) -> str:
        """Origin that serves OpenAI routes, e.g. http://127.0.0.1:11434.
        The proxy appends /v1/chat/completions."""

    async def activate(self, spec: ModelSpec) -> None:
        """Make this model the one that serves. Returns when it is loading;
        readiness is decided by health()."""

    async def stop(self) -> None:
        """Release whatever activate() acquired. Idempotent."""

    async def health(self) -> bool:
        """True when the active model can serve a request right now."""

    async def progress_hint(self) -> str | None:
        """Human-readable load progress for /admin/state, or None."""

    async def resources(self) -> list[ResourceInfo]:
        """GPU/accelerator state. May be empty — that is valid."""

    async def await_released(self, timeout_s: float) -> None:
        """Block until what the last activation held has actually been freed.

        The generalisation of vLLM's `wait_for_vram_release()`: on `ollama` it is
        polling `/api/ps` until the old tag is gone, on `remote_openai` it is
        nothing at all.

        This is a **correctness guard, not a memory-safety one**. The local
        machine has enough RAM to hold both models at once, so a load that
        overlaps an incomplete unload does not thrash — it quietly succeeds, and
        leaves two models resident while the supervisor reports one active. The
        single-active invariant is then false and nothing says so. That silence
        is the whole reason this method exists.

        Raises `BackendError` if the resources are still held at `timeout_s`.
        Failing the activation is better than proceeding into a state the server
        is lying about."""

    async def is_available(self, spec: ModelSpec) -> bool:
        """Whether this model can be served without first fetching it.

        For `ollama` that is "the tag is pulled", which is `/api/tags` — not
        `/api/ps`, which lists only what is loaded right now."""

    def upstream_headers(self) -> Mapping[str, str]:
        """Headers the proxy must add to every `/v1` request it relays.

        Empty for a backend we own the process of: `ollama` and `vllm` listen on
        `127.0.0.1` and authenticate nobody. A `remote_openai` upstream is
        somebody else's HTTPS endpoint and generally wants a bearer token, and
        the proxy has no way to supply one without asking.

        This exists because the alternative is `proxy.py` branching on
        `backend.name`, which `.claude/rules/backend-boundary.md` forbids: if a
        caller needs to know which backend it has, the interface is missing a
        method. This is that method.

        Never our own `HARNESS_API_KEY` — that authenticates the desktop app to
        us, and forwarding it would hand our key to a third party.
        """

    async def aclose(self) -> None:
        """Release transport resources (HTTP clients, pipes). Idempotent.

        Distinct from stop(): stop() unloads the model, aclose() releases what the
        backend object itself holds. The supervisor calls it when it discards a
        backend, so a switch does not leak a connection pool per activation."""


class BackendError(Exception):
    """A backend could not do what it was asked.

    The supervisor turns this into `state = error` and a `last_error` string; the
    backend itself never touches state.
    """
