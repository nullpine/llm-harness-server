"""The Backend Protocol — the contract between the supervisor and every engine.

Written out in `docs/BACKENDS.md` §1; the signatures here must match it exactly.
A backend owns only *how a model is made to serve*. It never sets state, never
decides policy, and never knows about the state machine — see
`.claude/rules/backend-boundary.md`.
"""

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
