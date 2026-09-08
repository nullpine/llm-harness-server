"""The `vllm` backend — Azure GPU VM, later (`docs/BACKENDS.md` §2.2).

Written to spec and kept lint- and type-clean, but **not exercised against a real
GPU until quota exists**. It is here so the abstraction is real rather than
aspirational; do not let it rot (CLAUDE.md § Notes on vLLM).

This is the only module in the package permitted to spawn a process
(`.claude/rules/process-safety.md`). Everything about the kill path exists because
a surviving vLLM holds 30+ GB and the next activation OOMs:

  * spawn with ``start_new_session=True`` so we own the process group
  * kill the **group**, SIGTERM then SIGKILL after 15 s
  * wait for the VRAM to actually come back before the next spawn
"""

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

import httpx

from harness_control.catalog import ModelSpec
from harness_control.logbuf import LogBuffer
from harness_control.supervisor import gpu
from harness_control.supervisor.backends.base import BackendError, ResourceInfo

log = logging.getLogger(__name__)

SIGKILL_GRACE_S = 15.0
PORT_FREE_TIMEOUT_S = 30.0


class VllmBackend:
    """Owns exactly one `vllm serve` process group at a time."""

    name = "vllm"

    def __init__(
        self,
        *,
        binary: Path,
        port: int = 8000,
        host: str = "127.0.0.1",
        logbuf: LogBuffer | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._binary = binary
        self._port = port
        self._host = host
        # `is None`, not `or`: LogBuffer defines __len__, so a *fresh* buffer is
        # falsy and `or` would silently discard the one the caller passed —
        # which is exactly how vLLM's stdout ended up in an orphan buffer.
        self._logbuf = LogBuffer() if logbuf is None else logbuf
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = client is None
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task[None] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def logbuf(self) -> LogBuffer:
        return self._logbuf

    async def activate(self, spec: ModelSpec) -> None:
        """Spawn `vllm serve` for `spec`. Returns as soon as it is starting."""
        await self.stop()
        # Refusing here is better than launching into an OOM three minutes later.
        try:
            await gpu.wait_for_vram_release()
        except TimeoutError as exc:
            raise BackendError(str(exc)) from exc

        argv = self.command(spec)
        log.info("spawning vllm: %s", " ".join(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # we own the process group; see the module docstring
            )
        except OSError as exc:
            raise BackendError(f"could not start {argv[0]}: {exc}") from exc
        self._proc = proc
        self._pump = asyncio.create_task(self._pump_output(proc))

    def command(self, spec: ModelSpec) -> list[str]:
        """The argv for `spec`.

        Model-specific flags come from `models.yaml`, never from here (CLAUDE.md
        § Notes on vLLM) — `--served-model-name` included. The proxy addresses the
        upstream by `model_ref`, so vLLM serving under its repo name is exactly
        right; a catalog that wants a different served name puts it in `args`.
        """
        return [
            str(self._binary),
            "serve",
            spec.model_ref,
            "--host",
            self._host,
            "--port",
            str(self._port),
            *spec.args,
        ]

    async def stop(self) -> None:
        """Kill the process group and wait for the port to come free. Idempotent."""
        proc, self._proc = self._proc, None
        pump, self._pump = self._pump, None
        if proc is not None and proc.returncode is None:
            await self._kill_group(proc)
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        await self._wait_for_port_free()

    async def health(self) -> bool:
        """True when vLLM's own `/health` answers 200. Bound to localhost only."""
        if self._proc is None or self._proc.returncode is not None:
            return False
        try:
            response = await self._client.get(f"{self.base_url}/health", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.is_success

    async def await_released(self, timeout_s: float) -> None:
        """Wait for the VRAM to actually come back before the next spawn.

        The original of the pattern the Protocol generalises: a vLLM process that
        has exited can still hold 30+ GB while the driver tears the context down,
        and spawning into that OOMs (`.claude/rules/process-safety.md`).
        """
        try:
            await gpu.wait_for_vram_release(timeout_s=timeout_s)
        except TimeoutError as exc:
            raise BackendError(str(exc)) from exc

    async def is_available(self, spec: ModelSpec) -> bool:
        """Whether the weights are already on disk.

        A Hugging Face repo that has not been downloaded is still *servable* —
        vLLM will fetch it — but not without a long wait, which is exactly what
        the desktop app wants to warn about. `scripts/download-models.sh` is what
        makes this true ahead of time.
        """
        cache = os.environ.get("HF_HOME")
        if not cache:
            return False
        # HF lays repos out as models--org--name under the hub directory.
        marker = f"models--{spec.model_ref.replace('/', '--')}"
        return (Path(cache) / "hub" / marker).exists()

    async def progress_hint(self) -> str | None:
        """The most recent line that looks like load progress, if any."""
        for line in reversed(self._logbuf.tail(50)):
            lowered = line.lower()
            if any(word in lowered for word in ("loading", "downloading", "capturing", "%")):
                return line.strip()[:200]
        return None

    async def resources(self) -> list[ResourceInfo]:
        return await gpu.query_gpus()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def upstream_headers(self) -> dict[str, str]:
        """Our own process, bound to 127.0.0.1 (SPEC §5.2). No token to send."""
        return {}

    async def aclose(self) -> None:
        await self.stop()
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ kill

    async def _kill_group(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the group, SIGKILL it after the grace period. Never just the pid."""
        for sig, wait_s in ((signal.SIGTERM, SIGKILL_GRACE_S), (signal.SIGKILL, 5.0)):
            if not self._signal_group(proc, sig):
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait_s)
                return
            except TimeoutError:
                log.warning("vllm pid %s survived %s; escalating", proc.pid, sig.name)
        log.error("vllm pid %s survived SIGKILL", proc.pid)

    def _signal_group(self, proc: asyncio.subprocess.Process, sig: signal.Signals) -> bool:
        """Signal the whole group. False when the process is already gone."""
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            return False
        except PermissionError as exc:  # pragma: no cover - needs a foreign process
            log.error("not permitted to signal vllm group %s: %s", proc.pid, exc)
            return False
        return True

    async def _wait_for_port_free(self) -> None:
        """Poll until nothing is listening on our port, or give up loudly."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PORT_FREE_TIMEOUT_S
        while await _port_in_use(self._host, self._port):
            if loop.time() >= deadline:
                raise BackendError(
                    f"something is still listening on {self._host}:{self._port} after "
                    f"{PORT_FREE_TIMEOUT_S:.0f}s; refusing to spawn a second vllm"
                )
            await asyncio.sleep(0.5)

    async def _pump_output(self, proc: asyncio.subprocess.Process) -> None:
        """Feed vLLM's stdout into the ring buffer for `/admin/logs` and the journal."""
        stream = proc.stdout
        if stream is None:  # pragma: no cover - we always ask for a pipe
            return
        while True:
            raw = await stream.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            self._logbuf.append(line)
            log.debug("vllm: %s", line)


async def _port_in_use(host: str, port: int) -> bool:
    """True when a TCP connect succeeds — i.e. someone is still holding the port."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def kill_orphan_on_port(host: str, port: int) -> None:
    """Kill whatever is holding the vLLM port at control-plane startup.

    SPEC §5.2: "On startup the supervisor kills any orphan listening on 8000 before
    doing anything else." Best effort — `lsof` may not exist, and the holder may not
    be ours to kill; both are logged, neither is fatal.
    """
    if not await _port_in_use(host, port):
        return
    lsof = _which("lsof")
    if lsof is None:
        log.warning("port %s:%s is busy and lsof is unavailable; cannot clear it", host, port)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            lsof,
            "-ti",
            f"tcp:{port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except (OSError, TimeoutError) as exc:
        log.warning("could not identify the holder of port %s: %s", port, exc)
        return

    for token in stdout.decode().split():
        try:
            pid = int(token)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            log.warning("killed orphan process group %s holding port %s", pid, port)
        except (ValueError, OSError) as exc:
            log.warning("could not kill orphan %s on port %s: %s", token, port, exc)


def _which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)
