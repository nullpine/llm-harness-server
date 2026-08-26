# Inference backends

How the control plane talks to whatever is actually running the model, and exactly
what changes when you move between them.

**The rule this document exists to protect:** the desktop app must never be able to
tell which backend is serving. `docs/API-CONTRACT.md` is identical in every
configuration below. Migrating backends is a server-side change and a URL in
Settings — never a client code change.

---

## 1. The interface

`src/harness_control/supervisor/backends/base.py`

```python
class ResourceInfo(TypedDict):
    index: int
    name: str
    memory_used_mb: int
    memory_total_mb: int
    utilization_pct: int


class Backend(Protocol):
    name: str                      # "ollama" | "vllm" | "remote_openai"

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
        Raises BackendError if it is still held at timeout_s."""

    async def is_available(self, spec: ModelSpec) -> bool:
        """Whether this model can be served without first fetching it."""

    async def aclose(self) -> None:
        """Release transport resources (HTTP clients, pipes). Idempotent."""
```

`await_released()` is the generalisation of vLLM's `wait_for_vram_release()`. On
`ollama` it polls `/api/ps` until the old tag is gone; on `remote_openai` it is a
no-op.

It is a **correctness guard, not a memory-safety one.** The local machine holds
48 GB, so GLM (~18 GB) and Qwen (~16 GB) both fit — a load overlapping an
incomplete unload does not thrash. It quietly succeeds, and leaves two models
resident while `/admin/state` reports one active. The single-active invariant is
then false and nothing surfaces it, which is exactly why the check has to be
explicit rather than left to memory pressure to reveal.

For the same reason `OLLAMA_MAX_LOADED_MODELS=1` is asserted at startup and the
control plane refuses to start without it: on a machine with room to spare, a
wrong value has no symptom at all.

`is_available()` answers "can this be served without a fetch" — for `ollama`,
`/api/tags`. Note it is *not* `/api/ps`: that lists what is loaded right now, so
using it would report every non-active model as unavailable.

`aclose()` is not `stop()`. `stop()` unloads the model; `aclose()` releases what the
backend object itself holds. The supervisor calls it whenever it discards a backend,
which is every activation — without it each model switch leaks a connection pool.

The **supervisor keeps everything else**: the state machine, the activation lock,
the drain, the job registry, the watchdog. A backend never decides state; it only
reports facts the supervisor turns into state.

---

## 2. The three implementations

### 2.1 `ollama` — local, today

Ollama runs as a daemon on `127.0.0.1:11434` and serves OpenAI-compatible routes at
`/v1/chat/completions`, `/v1/models`, with streaming. It manages weights itself, so
there is no process to spawn and no weight directory for us to own.

| Operation | How |
|---|---|
| activate | `POST /api/generate` `{"model": "<tag>", "keep_alive": -1}` with no prompt — preloads and pins |
| stop | `POST /api/generate` `{"model": "<tag>", "keep_alive": 0}` — unloads immediately |
| health | `GET /api/ps` lists loaded models; ready when our tag appears |
| progress_hint | parse the pull/load progress stream if a `pull` is in flight, else `None` |
| resources | `[]` on Apple Silicon — no `nvidia-smi`, and unified memory is not worth modelling in the MVP |

**Required daemon config.** Set `OLLAMA_MAX_LOADED_MODELS=1`. Ollama defaults to 3
concurrent models, which would silently break the single-active-model invariant —
the dropdown would say one thing while the daemon held three. Enforce it at the
daemon, not just in our supervisor.

Also relevant: `OLLAMA_KEEP_ALIVE` sets the global default, but the per-request
`keep_alive` we send overrides it. We always send it explicitly rather than relying
on daemon config.

**The proxy addresses the engine by `model_ref`, not by our catalog id.** A client
asks for `glm-4.7-flash`; Ollama only answers to `glm-4.7-flash:q4_K_M` and 404s on
anything else. `proxy.py` substitutes `supervisor.active_model_ref` into the outgoing
request body — backend-agnostically, since `model_ref` is by definition the name the
engine knows. Response frames therefore carry the *engine's* name
(`"model":"glm-4.7-flash:q4_K_M"`), not ours. That is deliberate and permanent:
rewriting frames on the way out would mean parsing and re-serialising the SSE stream,
which is exactly the buffering `aiter_raw()` exists to prevent
(`.claude/rules/streaming.md`). Clients key off the request they made, not off the
frame. This is also why no backend passes `--served-model-name` to rename itself back.

**Not supported by Ollama:** `logprobs`, `logit_bias`, `n`, `tool_choice`. None are
used by the MVP. Tool calling is post-MVP and will need `vllm` or `remote_openai`.

### 2.2 `vllm` — Azure GPU VM, later

Unchanged from the original spec. Spawns `vllm serve` as a process group on
`127.0.0.1:8000`, polls `/health`, kills the group on stop, and waits for VRAM to
actually release before the next activation. See `docs/SPEC.md` §5.2 and
`.claude/rules/process-safety.md` — all of that survives intact.

### 2.3 `remote_openai` — hosted provider, or our own remote deployment

The thinnest backend. `base_url` points at an external origin; `activate` and `stop`
are no-ops beyond verifying the model appears in the upstream `/v1/models`.
`health` is a `GET /v1/models` with a short timeout. `resources` returns `[]`.

The upstream API key comes from the environment, never from `models.yaml`.

Note the asymmetry: with this backend the control plane is a **thin auth and admin
layer**, not a supervisor. That is fine — the contract is what matters, and holding
the shape means the desktop app is unaffected.

---

## 3. `models.yaml`

```yaml
defaults:
  backend: ollama

models:
  - id: glm-4.7-flash
    display_name: GLM 4.7 Flash
    backend: ollama
    model_ref: glm-4.7-flash:q4_K_M      # the ollama tag
    params: 30B-A3B (MoE)
    quantization: q4_K_M
    context_length: 32768
    estimated_load_seconds: 25

  - id: qwen3.8-27b
    display_name: Qwen 3.8 27B
    backend: ollama
    model_ref: qwen3.8:27b-q4_K_M
    params: 27B (dense)
    quantization: q4_K_M
    context_length: 32768
    estimated_load_seconds: 35
```

`model_ref` replaces the old `hf_repo`, because what it names depends on the
backend: an Ollama tag, a Hugging Face repo, or a provider's model string.
`args` stays, and is passed through only by backends that can use it (`vllm`).

**Invariant: `model_ref` must be exactly what the backend serves the model under.**
The proxy addresses upstreams by `model_ref`, so anything that changes the engine's
served name — `--served-model-name` for vLLM, a retag for Ollama — must change
`model_ref` to match, or the proxy 404s.

**`estimated_load_seconds` is per-backend.** Ollama loading 18 GB from local SSD is
10–30 s; vLLM cold-starting the same model on an H100 is 60–120 s. The number the
desktop app shows in its switch dialog comes from here, so it must be honest for
the configuration actually in use.

---

## 4. Migrating to a remote model

Three scenarios. All of them leave `docs/API-CONTRACT.md`, the desktop repo, and
the state machine untouched.

### 4.1 Local Ollama → your own vLLM on an Azure GPU VM

This is a **deployment move**, not a code change. The control plane relocates from
your Mac to the VM and takes vLLM with it.

**Prerequisite, and it has lead time:** pay-as-you-go subscription, plus quota on
both `Standard NCADS_H100_v5 Family vCPUs` **and** the separate spot vCPU quota.
Start this before you need it.

| Step | Change |
|---|---|
| 1 | Run `scripts/provision.sh --host harness.example.com` on the VM. It installs vLLM, Caddy, the systemd unit, and downloads weights |
| 2 | In `models.yaml`: `backend: vllm`, and `model_ref` becomes the HF repo (`zai-org/GLM-4.7-Flash`) |
| 3 | Restore `args:` per model — `--tool-call-parser=glm47`, `--reasoning-parser=glm45`. Not `--served-model-name`: the proxy addresses vLLM by `model_ref`, so renaming it back would 404 (§2.1) |
| 4 | Raise `estimated_load_seconds` to the vLLM figures (75 / 110) |
| 5 | In the desktop app's Settings, change Server URL from `http://localhost:8080` to `https://harness.example.com`, and paste the API key `provision.sh` printed |

Nothing else. No rebuild of the desktop app, no contract change, no new IPC channel.
Conversations already on disk keep working; the `modelId` recorded on past messages
still resolves because the catalog ids are unchanged.

**What you gain:** FP8 instead of 4-bit, full throughput, tool-call and reasoning
parsers, and the machine is not your laptop.
**What you take on:** ~$1.29/hr on spot, eviction handling, TLS, and the operational
surface in `docs/OPERATIONS.md`.

### 4.2 Local Ollama → a hosted provider

The control plane stays wherever it is; only the backend changes.

| Step | Change |
|---|---|
| 1 | `models.yaml`: `backend: remote_openai`, `model_ref` = the provider's model string |
| 2 | Set `HARNESS_REMOTE_BASE_URL` and `HARNESS_REMOTE_API_KEY` in the environment |
| 3 | `estimated_load_seconds: 0` — activation is instant |

The desktop app sees model switching that completes in under a second instead of
half a minute. Everything else is identical.

### 4.3 Mixed catalog

Because `backend` is per-model, a catalog can hold both — a local model for private
work and a hosted one for everyday speed:

```yaml
models:
  - id: glm-4.7-flash-local
    backend: ollama
    model_ref: glm-4.7-flash:q4_K_M
  - id: glm-4.7-flash-hosted
    backend: remote_openai
    model_ref: z-ai/glm-4.7-flash
```

The supervisor stops the current backend before activating the next, so switching
from local to hosted unloads the local weights. The single-active-model rule holds
across backends, which is exactly what makes this predictable.

---

## 5. What stays true in every configuration

- `docs/API-CONTRACT.md` — byte-identical, hash-checked against the desktop repo
- The state machine: `idle → loading → ready → stopping`, `error` on failure
- Bearer auth on every route except `/healthz`
- One model active at a time
- The streaming proxy's anti-buffering rules (`aiter_raw`, no compression on `/v1`,
  `flush_interval -1` where Caddy is in the path)
- Client disconnect cancels the upstream request

## 6. What is deliberately not abstracted

`gpu.py` and `wait_for_vram_release()` stay specific to `VllmBackend`. Generalising
them to "accelerator resources" across CUDA, Metal, and nothing-at-all would produce
an abstraction with one real implementation and two empty ones. The `resources()`
method returning `[]` is the honest version.
