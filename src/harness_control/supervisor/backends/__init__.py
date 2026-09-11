"""Registry: backend name → implementation.

This is the only place that maps a `models.yaml` `backend:` string to a class.
Nothing outside this package may branch on which backend it has
(`.claude/rules/backend-boundary.md`); if a caller needs to know, the Protocol is
missing a method.
"""

from collections.abc import Callable, Mapping

from harness_control.logbuf import LogBuffer
from harness_control.settings import Settings
from harness_control.supervisor.backends.base import Backend, BackendError, ResourceInfo
from harness_control.supervisor.backends.ollama import OllamaBackend
from harness_control.supervisor.backends.remote import RemoteOpenAIBackend
from harness_control.supervisor.backends.vllm import VllmBackend

__all__ = [
    "BACKENDS",
    "Backend",
    "BackendError",
    "OllamaBackend",
    "RemoteOpenAIBackend",
    "ResourceInfo",
    "UnknownBackendError",
    "VllmBackend",
    "backend_names",
    "create_backend",
]


class UnknownBackendError(BackendError):
    """`models.yaml` names a backend that does not exist."""


# Every factory takes the supervisor's log buffer, even the two that have no use
# for it. A uniform signature is what lets `create_backend` stay ignorant of which
# backend it is building (`.claude/rules/backend-boundary.md`).


def _ollama(settings: Settings, logbuf: LogBuffer | None) -> Backend:
    """The daemon owns its own logs; there is no pipe for us to pump."""
    del logbuf
    return OllamaBackend(settings.ollama_url)


def _vllm(settings: Settings, logbuf: LogBuffer | None) -> Backend:
    """The buffer matters here: `vllm.py` pumps the engine's stdout into it.

    Without it the backend made a private LogBuffer, `_pump_output` filled that,
    and `/admin/logs` showed the supervisor's — a different object. Every line
    vLLM printed went somewhere nothing could read, so a load that failed was
    undiagnosable from the API, which is the one moment the desktop app offers
    "View server logs".
    """
    return VllmBackend(binary=settings.vllm_bin, port=settings.vllm_port, logbuf=logbuf)


def _remote(settings: Settings, logbuf: LogBuffer | None) -> Backend:
    """Someone else's process, so someone else's logs."""
    del logbuf
    return RemoteOpenAIBackend(settings.remote_base_url, settings.remote_api_key)


#: name → factory. Keyed by the string that appears in `models.yaml`.
BACKENDS: Mapping[str, Callable[[Settings, LogBuffer | None], Backend]] = {
    OllamaBackend.name: _ollama,
    VllmBackend.name: _vllm,
    RemoteOpenAIBackend.name: _remote,
}


def backend_names() -> list[str]:
    return sorted(BACKENDS)


def create_backend(name: str, settings: Settings, logbuf: LogBuffer | None = None) -> Backend:
    """Build the backend called `name`, or raise `UnknownBackendError`.

    `logbuf` is the supervisor's buffer, the one `/admin/logs` serves. Pass it so
    a backend that pumps an engine's output writes where operators can read it.
    """
    factory = BACKENDS.get(name)
    if factory is None:
        raise UnknownBackendError(f"unknown backend {name!r}; known backends: {backend_names()}")
    return factory(settings, logbuf)
