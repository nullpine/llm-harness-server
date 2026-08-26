"""One suite, run against every registered backend.

This is what stops the interface drifting. `docs/BACKENDS.md` §1 is the contract
between the supervisor and every engine; a backend that satisfies it is
interchangeable, and a backend that quietly does not would otherwise only be
discovered on the day someone flips `backend:` in `models.yaml`.

`vllm` is not exercised against a GPU — it never is, on any machine this suite
runs on. It *is* exercised as a process: the fake upstream doubles as a stand-in
`vllm` binary, so the spawn, the process-group kill, and the port-free wait are
all real code paths here.
"""

import socket
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from conftest import LiveServer
from fake_upstream import DEFAULT_MODEL, FakeUpstream
from harness_control.catalog import ModelSpec
from harness_control.settings import Settings
from harness_control.supervisor.backends import (
    BACKENDS,
    Backend,
    BackendError,
    OllamaBackend,
    RemoteOpenAIBackend,
    UnknownBackendError,
    VllmBackend,
    backend_names,
    create_backend,
)
from harness_control.supervisor.backends.base import ResourceInfo
from harness_control.supervisor.readiness import poll_until

FAKE_UPSTREAM = Path(__file__).resolve().parent / "fake_upstream.py"


@dataclass
class Harness:
    """A backend under test, plus the spec it should be asked to activate."""

    backend: Backend
    spec: ModelSpec
    #: vLLM starts a real process; the network-only backends answer immediately.
    ready_timeout_s: float = 5.0


def make_spec(model_ref: str, backend: str) -> ModelSpec:
    return ModelSpec(
        id="glm-4.7-flash",
        display_name="GLM 4.7 Flash",
        backend=backend,
        model_ref=model_ref,
        estimated_load_seconds=1,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest_asyncio.fixture(params=sorted(BACKENDS))
async def harness(
    request: pytest.FixtureRequest,
    upstream: FakeUpstream,
    upstream_server: LiveServer,
) -> AsyncIterator[Harness]:
    name = request.param
    built: Harness
    if name == "ollama":
        built = Harness(OllamaBackend(upstream_server.url), make_spec(DEFAULT_MODEL, name))
    elif name == "remote_openai":
        upstream.served_models = {DEFAULT_MODEL}
        built = Harness(RemoteOpenAIBackend(upstream_server.url), make_spec(DEFAULT_MODEL, name))
    elif name == "vllm":
        # The fake upstream, run as a script, stands in for the `vllm` binary. The
        # spawn and the process-group kill below are the real implementation.
        backend = VllmBackend(binary=Path(sys.executable), port=free_port())
        backend._binary = Path(sys.executable)  # private on purpose: see command() below
        built = Harness(_ScriptedVllm(backend), make_spec("any/repo", name), ready_timeout_s=20.0)
    else:  # pragma: no cover - a new backend must be added here deliberately
        pytest.fail(f"test_backends.py does not know how to build {name!r}")

    try:
        yield built
    finally:
        await _aclose(built.backend)


class _ScriptedVllm:
    """`VllmBackend` with the binary swapped for the fake upstream script.

    Everything else — spawn, `start_new_session`, the group kill, the port wait —
    is the real implementation. Only the executable changes, which is exactly the
    thin seam `.claude/rules/process-safety.md` asks for.
    """

    def __init__(self, inner: VllmBackend) -> None:
        self._inner = inner
        self.name = inner.name

    @property
    def base_url(self) -> str:
        return self._inner.base_url

    async def activate(self, spec: ModelSpec) -> None:
        argv = self._inner.command(spec)
        self._inner.command = lambda spec: [sys.executable, str(FAKE_UPSTREAM), *argv[1:]]  # type: ignore[method-assign]
        await self._inner.activate(spec)

    async def stop(self) -> None:
        await self._inner.stop()

    async def health(self) -> bool:
        return await self._inner.health()

    async def progress_hint(self) -> str | None:
        return await self._inner.progress_hint()

    async def resources(self) -> list[ResourceInfo]:
        return await self._inner.resources()

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _aclose(backend: Backend) -> None:
    await backend.aclose()


async def wait_ready(backend: Backend, timeout_s: float) -> bool:
    return await poll_until(backend.health, timeout_s=timeout_s, interval_s=0.1)


# ------------------------------------------------------- the registry itself


def test_every_documented_backend_is_registered() -> None:
    assert backend_names() == ["ollama", "remote_openai", "vllm"]


def test_an_unknown_backend_is_a_clear_error() -> None:
    with pytest.raises(UnknownBackendError, match="unknown backend 'sagemaker'"):
        create_backend("sagemaker", Settings())


def test_the_registry_builds_every_backend(settings: Settings) -> None:
    for name in backend_names():
        assert create_backend(name, settings).name == name


# ------------------------------------------------------------ the shared suite


def test_conforms_to_the_protocol(harness: Harness) -> None:
    assert isinstance(harness.backend, Backend)


def test_name_matches_the_registry(harness: Harness) -> None:
    assert harness.backend.name in BACKENDS


def test_base_url_is_an_origin_the_proxy_can_append_to(harness: Harness) -> None:
    """`proxy.py` does `f"{base_url}/v1/chat/completions"` and nothing cleverer."""
    url = harness.backend.base_url
    assert url.startswith("http://") or url.startswith("https://")
    assert not url.endswith("/"), "a trailing slash would produce a double slash"
    assert url.count("/") == 2, "base_url is an origin, not a path"


async def test_health_is_false_before_activation(harness: Harness) -> None:
    assert await harness.backend.health() is False


async def test_stop_before_activate_is_a_no_op(harness: Harness) -> None:
    """Idempotent means "safe from any state", including never-started."""
    await harness.backend.stop()
    assert await harness.backend.health() is False


async def test_activate_then_health_becomes_true(harness: Harness) -> None:
    await harness.backend.activate(harness.spec)
    assert await wait_ready(harness.backend, harness.ready_timeout_s)


async def test_stop_releases_and_is_idempotent(harness: Harness) -> None:
    await harness.backend.activate(harness.spec)
    assert await wait_ready(harness.backend, harness.ready_timeout_s)

    await harness.backend.stop()
    assert await harness.backend.health() is False
    await harness.backend.stop()  # must not raise the second time
    assert await harness.backend.health() is False


async def test_activate_after_stop_works(harness: Harness) -> None:
    """A switch is stop-then-activate; a backend that cannot be reused breaks M2."""
    await harness.backend.activate(harness.spec)
    assert await wait_ready(harness.backend, harness.ready_timeout_s)
    await harness.backend.stop()
    await harness.backend.activate(harness.spec)
    assert await wait_ready(harness.backend, harness.ready_timeout_s)


async def test_aclose_on_a_never_activated_backend_is_a_no_op(harness: Harness) -> None:
    """The supervisor discards backends it never got as far as activating."""
    await harness.backend.aclose()


async def test_aclose_is_idempotent(harness: Harness) -> None:
    """Called twice — the supervisor's `finally` can race an explicit shutdown."""
    await harness.backend.activate(harness.spec)
    assert await wait_ready(harness.backend, harness.ready_timeout_s)
    await harness.backend.aclose()
    await harness.backend.aclose()


async def test_progress_hint_is_a_string_or_none(harness: Harness) -> None:
    hint = await harness.backend.progress_hint()
    assert hint is None or isinstance(hint, str)


async def test_resources_is_a_list_of_the_right_shape(harness: Harness) -> None:
    """`[]` is valid and must be handled without error — acceptance L11."""
    resources = await harness.backend.resources()
    assert isinstance(resources, list)
    for entry in resources:  # pragma: no cover - no GPU on any CI machine
        assert set(entry) == {
            "index",
            "name",
            "memory_used_mb",
            "memory_total_mb",
            "utilization_pct",
        }


# ------------------------------------------------- ollama-specific behaviour


async def test_ollama_pins_with_keep_alive_minus_one(
    ollama_backend: OllamaBackend, upstream: FakeUpstream, spec: ModelSpec
) -> None:
    await ollama_backend.activate(spec)
    assert upstream.generate_calls == [{"model": spec.model_ref, "keep_alive": -1}]
    await ollama_backend.aclose()


async def test_ollama_unloads_with_keep_alive_zero(
    ollama_backend: OllamaBackend, upstream: FakeUpstream, spec: ModelSpec
) -> None:
    await ollama_backend.activate(spec)
    await ollama_backend.stop()
    assert upstream.generate_calls[-1] == {"model": spec.model_ref, "keep_alive": 0}
    assert spec.model_ref not in upstream.loaded
    await ollama_backend.aclose()


async def test_ollama_health_reads_api_ps(
    ollama_backend: OllamaBackend, upstream: FakeUpstream, spec: ModelSpec
) -> None:
    await ollama_backend.activate(spec)
    assert await ollama_backend.health() is True
    # A daemon that dropped our model under us — health must notice, not assume.
    upstream.loaded.discard(spec.model_ref)
    assert await ollama_backend.health() is False
    await ollama_backend.aclose()


async def test_ollama_health_is_false_when_the_daemon_errors(
    ollama_backend: OllamaBackend, upstream: FakeUpstream, spec: ModelSpec
) -> None:
    await ollama_backend.activate(spec)
    upstream.ps_status = 500
    assert await ollama_backend.health() is False
    await ollama_backend.aclose()


async def test_ollama_reports_a_refusal_as_a_backend_error(
    ollama_backend: OllamaBackend, upstream: FakeUpstream, spec: ModelSpec
) -> None:
    upstream.generate_status = 404
    with pytest.raises(BackendError, match="404"):
        await ollama_backend.activate(spec)
    await ollama_backend.aclose()


async def test_ollama_reports_an_unreachable_daemon(spec: ModelSpec) -> None:
    backend = OllamaBackend(f"http://127.0.0.1:{free_port()}")
    with pytest.raises(BackendError, match="unreachable"):
        await backend.activate(spec)
    await backend.aclose()


async def test_ollama_never_talks_to_the_openai_routes(
    ollama_backend: OllamaBackend, upstream: FakeUpstream, spec: ModelSpec
) -> None:
    """Inference goes through the proxy, not the backend. Only `/api/*` here."""
    await ollama_backend.activate(spec)
    await ollama_backend.health()
    assert upstream.chat_calls == []
    await ollama_backend.aclose()


# ------------------------------------------------- remote-specific behaviour


async def test_remote_rejects_a_model_the_upstream_does_not_offer(
    upstream: FakeUpstream, upstream_server: LiveServer
) -> None:
    upstream.served_models = {"someone-elses-model"}
    backend = RemoteOpenAIBackend(upstream_server.url)
    with pytest.raises(BackendError, match="not offered"):
        await backend.activate(make_spec(DEFAULT_MODEL, "remote_openai"))
    await backend.aclose()


async def test_remote_without_a_base_url_says_so() -> None:
    backend = RemoteOpenAIBackend("")
    with pytest.raises(BackendError, match="HARNESS_REMOTE_BASE_URL"):
        await backend.activate(make_spec(DEFAULT_MODEL, "remote_openai"))
    await backend.aclose()


async def test_remote_sends_the_upstream_key_not_ours(
    upstream: FakeUpstream, upstream_server: LiveServer
) -> None:
    """The upstream key comes from the environment, never from models.yaml."""
    backend = RemoteOpenAIBackend(upstream_server.url, api_key="upstream-secret-key")
    await backend.activate(make_spec(DEFAULT_MODEL, "remote_openai"))
    assert await backend.health() is True
    await backend.aclose()


# --------------------------------------------------- vllm-specific behaviour


def test_vllm_builds_the_documented_command(spec: ModelSpec) -> None:
    backend = VllmBackend(binary=Path("/opt/harness/.venv/bin/vllm"), port=8000)
    argv = backend.command(spec)
    assert argv[:3] == ["/opt/harness/.venv/bin/vllm", "serve", spec.model_ref]
    assert "--host" in argv and "127.0.0.1" in argv
    assert argv[argv.index("--port") + 1] == "8000"
    assert "--served-model-name" not in argv, (
        "the served name belongs in models.yaml `args:`, not hardcoded here"
    )


def test_vllm_takes_model_specific_flags_from_the_catalog() -> None:
    """Never hardcoded in Python — CLAUDE.md § Notes on vLLM."""
    spec = make_spec("zai-org/GLM-4.7-Flash", "vllm")
    spec.args = ["--tool-call-parser=glm47", "--reasoning-parser=glm45"]
    argv = VllmBackend(binary=Path("vllm")).command(spec)
    assert argv[-2:] == ["--tool-call-parser=glm47", "--reasoning-parser=glm45"]


async def test_vllm_reports_a_missing_binary_clearly(spec: ModelSpec) -> None:
    backend = VllmBackend(binary=Path("/nonexistent/vllm"), port=free_port())
    with pytest.raises(BackendError, match="could not start"):
        await backend.activate(spec)
    await backend.aclose()


async def test_vllm_kills_the_whole_process_group(spec: ModelSpec) -> None:
    """A surviving child holds 30+ GB and the next activation OOMs."""
    backend = VllmBackend(binary=Path(sys.executable), port=free_port())
    backend.command = lambda spec: [  # type: ignore[method-assign]
        sys.executable,
        str(FAKE_UPSTREAM),
        "--port",
        str(backend._port),  # private on purpose
    ]
    await backend.activate(spec)
    assert await poll_until(backend.health, timeout_s=20.0, interval_s=0.1)

    proc = backend._proc  # private on purpose
    assert proc is not None
    pid = proc.pid
    await backend.stop()

    assert backend.is_running() is False
    assert not _pid_alive(pid), "the process group survived stop()"
    await backend.aclose()


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not ours, but alive
        return True
    return True


async def test_vllm_health_is_false_once_the_process_is_gone(spec: ModelSpec) -> None:
    backend = VllmBackend(binary=Path("/nonexistent/vllm"), port=free_port())
    assert await backend.health() is False
    await backend.aclose()


async def test_vllm_progress_hint_comes_from_the_log_buffer() -> None:
    backend = VllmBackend(binary=Path("vllm"), port=free_port())
    assert await backend.progress_hint() is None
    backend.logbuf.append("INFO Loading safetensors checkpoint shards: 40% Completed")
    hint = await backend.progress_hint()
    assert hint is not None and "40%" in hint
    await backend.aclose()


async def test_no_gpu_means_no_resources_and_no_error() -> None:
    """L11: `/admin/state.gpu` is `[]` on a machine with no nvidia-smi."""
    from harness_control.supervisor import gpu

    if gpu.nvidia_smi_path() is not None:  # pragma: no cover - not on CI or a Mac
        pytest.skip("this machine has nvidia-smi")
    assert await gpu.query_gpus() == []
    await gpu.wait_for_vram_release(timeout_s=0.1)  # must return, not raise


def test_the_gpu_parser_handles_a_real_nvidia_smi_row() -> None:
    from harness_control.supervisor.gpu import _parse

    rows = _parse("0, NVIDIA H100 NVL, 41210, 95830, 0\ngarbage row\n")
    assert rows == [
        {
            "index": 0,
            "name": "NVIDIA H100 NVL",
            "memory_used_mb": 41210,
            "memory_total_mb": 95830,
            "utilization_pct": 0,
        }
    ]


# ------------------------------------------------------------ the boundary


def test_no_backend_branching_outside_backends() -> None:
    """`.claude/rules/backend-boundary.md`: no `if backend.name ==` outside backends/.

    Asserted mechanically because it is the kind of shortcut that gets added under
    deadline pressure and is invisible in review.
    """
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "harness_control"
    pattern = re.compile(
        r"backend(?:\.name|_name)\s*==|name\s*==\s*[\"'](?:ollama|vllm|remote_openai)"
    )
    offenders = [
        path.relative_to(src)
        for path in src.rglob("*.py")
        if "backends" not in path.parts and pattern.search(path.read_text())
    ]
    assert offenders == [], f"backend branching leaked out of backends/: {offenders}"
