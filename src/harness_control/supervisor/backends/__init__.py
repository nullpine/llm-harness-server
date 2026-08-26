"""Registry: backend name → implementation.

This is the only place that maps a `models.yaml` `backend:` string to a class.
Nothing outside this package may branch on which backend it has
(`.claude/rules/backend-boundary.md`); if a caller needs to know, the Protocol is
missing a method.
"""

from collections.abc import Callable, Mapping

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


def _ollama(settings: Settings) -> Backend:
    return OllamaBackend(settings.ollama_url)


def _vllm(settings: Settings) -> Backend:
    return VllmBackend(binary=settings.vllm_bin, port=settings.vllm_port)


def _remote(settings: Settings) -> Backend:
    return RemoteOpenAIBackend(settings.remote_base_url, settings.remote_api_key)


#: name → factory. Keyed by the string that appears in `models.yaml`.
BACKENDS: Mapping[str, Callable[[Settings], Backend]] = {
    OllamaBackend.name: _ollama,
    VllmBackend.name: _vllm,
    RemoteOpenAIBackend.name: _remote,
}


def backend_names() -> list[str]:
    return sorted(BACKENDS)


def create_backend(name: str, settings: Settings) -> Backend:
    """Build the backend called `name`, or raise `UnknownBackendError`."""
    factory = BACKENDS.get(name)
    if factory is None:
        raise UnknownBackendError(f"unknown backend {name!r}; known backends: {backend_names()}")
    return factory(settings)
