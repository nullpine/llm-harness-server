---
description: Rules for spawning, killing, and tracking the single vLLM process
paths:
  - "src/harness_control/supervisor/**"
  - "deploy/systemd/**"
  - "scripts/**"
  - "tests/test_supervisor_activate.py"
  - "tests/test_state_machine.py"
---

# Process safety — exactly one vLLM, and no leaked GPU memory

## One process, one owner

Every spawn and kill goes through `supervisor/process.py`. No `subprocess.Popen`
or `asyncio.create_subprocess_exec` anywhere else in the package.

## Killing must actually kill

A vLLM process that survives holds 30+ GB of VRAM and the next activation OOMs.
All of these are required:

- spawn with `start_new_session=True` so we own the process group
- kill the **process group** (`os.killpg`), SIGTERM then SIGKILL after 15 s
- `harness-control.service` uses `KillMode=control-group`
- `gpu.wait_for_vram_release()` polls `nvidia-smi` before every spawn and **fails
  the activation with a clear error** rather than launching into an OOM
- on control-plane startup, kill any orphan holding the vLLM port

## No crash-loop restarts

A crashed model stays in `error` until a client activates something. Auto-restart
hides real failures. This is ADR-0005 — deliberate, not an oversight.

## Async discipline

No blocking calls in async paths. `nvidia-smi`, process waits, and file reads go
through `asyncio.create_subprocess_exec` or `run_in_executor`. A blocking call in
the event loop stalls every in-flight stream.

## Testing

The whole suite runs with **no GPU**. `tests/fake_vllm.py` stands in for vLLM;
nothing in `tests/` may import torch or require CUDA. If something cannot be tested
without a GPU, isolate it behind a thin seam and test around it.

Full detail: `docs/SPEC.md` §5.2.
