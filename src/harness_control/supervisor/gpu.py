"""`nvidia-smi` parsing, and waiting for VRAM to actually come back.

Deliberately not abstracted across accelerators (`docs/BACKENDS.md` §6): this is
CUDA-specific and belongs to the `vllm` backend. On a machine with no `nvidia-smi`
every function here degrades to "no GPUs", which is the honest answer on Apple
Silicon and keeps acceptance L11 (`/admin/state.gpu == []`) working.
"""

import asyncio
import logging
import shutil

from harness_control.supervisor.backends.base import ResourceInfo

log = logging.getLogger(__name__)

_QUERY = "index,name,memory.used,memory.total,utilization.gpu"
_SMI_TIMEOUT_S = 5.0


def nvidia_smi_path() -> str | None:
    """Where `nvidia-smi` is, or None on a machine without one."""
    return shutil.which("nvidia-smi")


async def query_gpus() -> list[ResourceInfo]:
    """Current GPU state, or `[]` when there is no NVIDIA driver to ask."""
    smi = nvidia_smi_path()
    if smi is None:
        return []
    try:
        stdout = await _run(smi, f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits")
    except (OSError, TimeoutError) as exc:
        log.warning("nvidia-smi failed; reporting no GPUs: %s", exc)
        return []
    return _parse(stdout)


async def total_memory_used_mb() -> int:
    """Sum of used VRAM across all GPUs. Zero when there are none."""
    return sum(gpu["memory_used_mb"] for gpu in await query_gpus())


async def wait_for_vram_release(
    *, threshold_mb: int = 2048, timeout_s: float = 60.0, poll_interval_s: float = 1.0
) -> None:
    """Block until VRAM has actually been released, or raise.

    A vLLM process that has exited can still hold 30+ GB for several seconds while
    the driver tears the context down. Spawning into that OOMs. Failing the
    activation with a clear error is strictly better than launching and dying
    (`.claude/rules/process-safety.md`).
    """
    if nvidia_smi_path() is None:
        return  # No GPU to wait for. Not an error — see docs/BACKENDS.md §6.

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    used_mb = await total_memory_used_mb()
    while used_mb > threshold_mb:
        if loop.time() >= deadline:
            raise TimeoutError(
                f"{used_mb} MB of VRAM still held after {timeout_s:.0f}s "
                f"(threshold {threshold_mb} MB); refusing to spawn into an OOM"
            )
        await asyncio.sleep(poll_interval_s)
        used_mb = await total_memory_used_mb()


async def _run(*argv: str) -> str:
    """Run a command off the event loop. No blocking calls in async paths."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_SMI_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return stdout.decode("utf-8", errors="replace")


def _parse(stdout: str) -> list[ResourceInfo]:
    """Parse `--format=csv,noheader,nounits`. A malformed row is skipped, not fatal."""
    gpus: list[ResourceInfo] = []
    for line in stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 5:
            continue
        index, name, used, total, util = fields
        try:
            gpus.append(
                ResourceInfo(
                    index=int(index),
                    name=name,
                    memory_used_mb=int(float(used)),
                    memory_total_mb=int(float(total)),
                    utilization_pct=int(float(util)),
                )
            )
        except ValueError:
            log.warning("unparseable nvidia-smi row: %r", line)
    return gpus
