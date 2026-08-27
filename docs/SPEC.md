# llm-harness-server — MVP Specification

**Version:** 0.1 (MVP)
**Repo:** `llm-harness-server`
**Companion repo:** `llm-harness-desktop`
**Contract:** `docs/API-CONTRACT.md`

---

## 1. What this is

Everything that runs on the Azure GPU VM: a small FastAPI **control plane** that
owns exactly one **vLLM** process at a time, plus the provisioning, TLS, and
systemd glue to get it running on a fresh VM.

It exists because raw vLLM cannot do the two things the desktop app needs:
authenticate a client, and change which model is loaded.

```
   Internet
      │  HTTPS :443
      ▼
 ┌────────────────────────── Azure VM (Ubuntu 24.04, 1× H100 NVL 94GB) ────────┐
 │  Caddy  ── auto TLS, HSTS, rate limit ──▶ 127.0.0.1:8080                    │
 │                                             │                               │
 │                                    control plane (FastAPI/uvicorn)          │
 │                                      ├── auth (bearer, constant time)       │
 │                                      ├── /v1/*  → streaming proxy ──┐       │
 │                                      ├── /admin/* → supervisor      │       │
 │                                      └── model catalog (models.yaml)│       │
 │                                                                     ▼       │
 │                                              vLLM OpenAI server 127.0.0.1:8000
 │                                                      │                       │
 │                                              weights on /mnt/models (NVMe)   │
 └──────────────────────────────────────────────────────────────────────────────┘
```

## 2. Goals

| # | Goal |
|---|---|
| G1 | Serve OpenAI-compatible chat completions, streaming, from a self-hosted open-weights model |
| G2 | Load exactly one model at a time and swap on request, reporting state honestly throughout |
| G3 | Refuse every unauthenticated request; never expose vLLM to the internet directly |
| G4 | Come up healthy after a VM reboot without a human |
| G5 | Adding a model is one entry in `models.yaml` plus a weight download |

## 3. Non-goals

- Multiple models resident at once; request-level routing between models
- Multi-tenant auth, per-user quotas, usage billing
- Autoscaling, multi-VM load balancing, and *graceful* spot-eviction handling
  (draining, checkpointing, migration). The VM **is** provisioned as Spot per
  ADR-0006, but an eviction is simply an outage the client recovers from
- Fine-tuning, LoRA hot-swap, RLHF weight updates
- Embeddings, reranking, audio, image generation endpoints
- A web UI (the desktop app is the only client)
- Terraform/Bicep-managed infrastructure — MVP provisions with a shell script;
  IaC is a post-MVP item
- Backend-specific features that would leak into the contract — tool calling,
  logprobs, vision. Ollama supports none of them; keeping them out of the MVP is
  what makes the backends interchangeable

## 4. Deployment targets

The inference engine is pluggable — see ADR-0007 and `docs/BACKENDS.md`.
Two targets are specified here.

| | Backend | Status |
|---|---|---|
| **Local — the MVP target** | `ollama` on Apple Silicon | what we build and test against now (§4.3) |
| Azure GPU VM | `vllm` on H100 | the production target, blocked on GPU quota (§4.1–4.2) |

Everything in §4.1 and §4.2 remains accurate for the Azure target. It is not the
current deployment. Migration steps are in `docs/BACKENDS.md` §4.1.

### 4.1 VM

| | Choice | Notes |
|---|---|---|
| SKU | `Standard_NC40ads_H100_v5`, **provisioned as Spot** | 1× **H100 NVL, 94 GB**, 40 vCPU, 320 GiB RAM. **$1.29/hr spot vs $6.98/hr on-demand.** Sibling `NC80adis_H100_v5` doubles it (2× GPU) if you later want both models resident |
| Eviction policy | `Deallocate`, max price = on-demand | Spot eviction stops the VM; it does not delete it. `/mnt/models` and the config survive |
| OS | Ubuntu 24.04 LTS + NVIDIA driver ≥ 550, CUDA 12.4+ | Use the Azure NVIDIA GPU-optimized image where available |
| Disk | OS 128 GB Premium SSD + **1 TB Premium SSD v2 mounted at `/mnt/models`** | Weights persist across reboots; do **not** use the ephemeral resource disk |
| Network | NSG allows 443 from your IP only; 22 via Azure Bastion or your IP | vLLM's 8000 and the control plane's 8080 bind to `127.0.0.1` and are never in the NSG |

**Cost.** This is the dominant design constraint, so it is stated plainly.

| Configuration | $/hr | 3 hr/day | 24/7 |
|---|---:|---:|---:|
| NC40ads H100 v5, on-demand | $6.98 | $628/mo | $5,095/mo |
| **NC40ads H100 v5, spot** | **$1.29** | **$116/mo** | $942/mo |
| NV36ads A10 v5, on-demand (smaller models only) | $3.20 | $288/mo | $2,336/mo |
| NV36ads A10 v5, spot | $0.59 | $53/mo | $431/mo |

Note the ordering: **spot H100 is cheaper than on-demand A10** and far more capable.
Dropping to a smaller GPU is the wrong lever; spot is the right one.

Three mitigations, all in scope for the MVP:

1. **Provision as Spot** with `--priority Spot --eviction-policy Deallocate
   --max-price -1`. Eviction gives ~30 s notice and stops the VM. The desktop app
   already models this — it shows `unreachable` and recovers when the VM returns.
   For a single-user personal harness, an occasional eviction is an inconvenience,
   not an outage.
2. **`scripts/vm-stop.sh`** (Azure CLI deallocate) plus a DevTest auto-shutdown
   schedule configured during provisioning. A deallocated VM costs only its disks
   (~$15/mo for 1 TB Premium SSD v2 + OS disk).
3. **Build against the mock.** The desktop repo's mock server implements the full
   contract, so the client work — which is most of the work — happens with the VM
   deallocated.

**Reality check, and it belongs in the spec rather than a footnote:** one person
chatting uses roughly 1–3 % of an H100's throughput. The same models are available
from hosted providers at ~$0.06/M input and ~$0.40/M output, which puts a heavy
personal workload in the range of $2–20/month. Self-hosting is the right call when
you need data to stay in your own tenant, a pinned model version, flat cost under
heavy agentic load, or — as here — the harness itself is the point. It is not the
cheaper option for one user, and the project should not pretend otherwise.

See ADR-0006.

### 4.2 Models (MVP catalog)

Both fit one H100 with FP8 weights, one at a time.

| id | HF repo | Shape | Weights (FP8) | Notes |
|---|---|---|---|---|
| `glm-4.7-flash` | `zai-org/GLM-4.7-Flash` | 30B total / 3B active, MoE | ~32 GB | Fast, strong at coding/tool use. Needs `--tool-call-parser glm47 --reasoning-parser glm45` |
| `qwen3.8-27b` | `Qwen/Qwen3.8-27B-FP8` | 27B dense, native VL, 262k ctx | ~30 GB | Vision inputs are **out of MVP scope** — serve text only |

**Do not attempt GLM-5 / GLM-5.2 / Qwen3.8-2.4T-A95B.** Those are 750B–2.4T
parameter models needing 8–32 GPUs; they are outside this MVP's hardware budget.
The catalog is YAML, so upgrading later is configuration, not code.

**VRAM budget, 94 GB card:** ~30 GB weights + ~4 GB activations/CUDA graphs leaves
roughly 55–60 GB for KV cache at `gpu_memory_utilization: 0.90`. That is generous
for a single user.

Set `--max-model-len` to `32768` for both in the MVP anyway. Qwen's full 262k
context is a KV-cache tuning exercise that needs a measured headroom check under
concurrent load — worth doing, but as a deliberate task with a benchmark behind it,
not as a starting default that quietly OOMs on a long conversation.

### 4.3 Local target (current)

| | Choice | Notes |
|---|---|---|
| Host | Apple Silicon Mac, 48 GB unified memory | holds both spec'd models at 4-bit *simultaneously* — see L5 |
| Engine | Ollama daemon on `127.0.0.1:11434` | OpenAI-compatible at `/v1`, manages weights itself |
| Control plane | `127.0.0.1:8080`, no TLS | the desktop app's settings validator already permits `http://localhost` |
| Auth | bearer key, same as production | keep it — it is the contract, and it makes the local and remote paths identical |
| Cost | zero | |

Required daemon configuration: **`OLLAMA_MAX_LOADED_MODELS=1`**. Ollama defaults
to three concurrent models, which would silently violate the single-active-model
invariant — the dropdown would claim one model while the daemon held three.

No Caddy, no systemd, no `provision.sh` on this path. `scripts/dev-local.sh`
starts the daemon and the control plane together.

| id | Ollama tag | Weights (q4_K_M) | Load |
|---|---|---|---|
| `glm-4.7-flash` | `glm-4.7-flash:q4_K_M` | ~18 GB | ~25 s |
| `qwen3.8-27b` | `qwen3.8:27b-q4_K_M` | ~16 GB | ~35 s |

## 5. Components

### 5.1 Control plane (`harness_control`)

Python 3.12, FastAPI, uvicorn, httpx (`AsyncClient` with `http2=False`, no response
buffering), pydantic-settings.

Modules:

| Module | Responsibility |
|---|---|
| `app.py` | FastAPI app factory, lifespan (start poller, restore last model), CORS off |
| `auth.py` | Bearer dependency; `hmac.compare_digest`; exempts `/healthz` |
| `catalog.py` | Loads and validates `models.yaml` into `ModelSpec` objects at startup |
| `supervisor.py` | The state machine. Owns the vLLM subprocess and the activation lock |
| `proxy.py` | `/v1/*` passthrough with true streaming and abort propagation |
| `admin.py` | `/admin/*` routes |
| `gpu.py` | `nvidia-smi --query-gpu=... --format=csv` parsed into the state payload |
| `logbuf.py` | Ring buffer (2000 lines) of vLLM stdout/stderr for `/admin/logs` |
| `settings.py` | Env config via pydantic-settings |

### 5.2 Supervisor semantics (the heart of the MVP)

State: `idle | loading | ready | stopping | error` (see the API contract for the
externally visible machine).

The supervisor owns the state machine, the activation lock, the drain, and the
job registry. **How** a model is made to serve belongs to the backend — see
`docs/BACKENDS.md` §1. The steps below describe the `vllm` backend; `ollama`
substitutes a preload call for the spawn and a `keep_alive: 0` for the kill,
and `remote_openai` makes both no-ops.

```
activate(model_id):
    acquire activation_lock (non-blocking; 409 activation_in_progress if held)
    if model_id == active and state == ready: return already_active
    job = new Job(model_id)
    spawn background task:
        state = stopping
        if a vLLM process exists:
            stop accepting /v1 (proxy returns 503 model_loading)
            wait for in-flight completions, max DRAIN_TIMEOUT_S (30)
            SIGTERM the process *group*; SIGKILL after 15 s
            wait for port 8000 to be free, max 30 s
        state = loading
        proc = spawn vllm serve <spec.hf_repo> <spec.args...> --port 8000 --host 127.0.0.1
               (start_new_session=True so we own the process group)
        pipe stdout/stderr into logbuf and the journal
        poll http://127.0.0.1:8000/health every 2 s until 200 or LOAD_TIMEOUT_S (900)
        on success: state = ready; active = model_id; persist to /var/lib/harness/last_model
        on timeout/exit: state = error; last_error = last 20 log lines; kill the process
    release lock
```

Rules:

- Exactly one vLLM process may exist. On startup the supervisor kills any orphan
  listening on 8000 before doing anything else.
- The control plane must survive vLLM crashing. A watchdog notices the process
  exited while `state == ready` and transitions to `error` within 5 s.
- **No crash-loop restarts in MVP.** A crashed model stays in `error` until the
  client activates something. (Auto-restart hides real failures; add it later
  with a backoff cap.)
- On control-plane startup, if `/var/lib/harness/last_model` exists, activate that
  model automatically so a reboot self-heals.

### 5.3 Streaming proxy

```python
# proxy.py — the shape that matters
async def chat_completions(request: Request):
    guard_state()                       # 409 / 503 per the contract
    body = await request.json()
    if body.get("model") != supervisor.active_model_id:
        raise ModelNotActive(...)
    req = client.build_request("POST", f"{VLLM}/v1/chat/completions", json=body)
    resp = await client.send(req, stream=True)
    if not body.get("stream"):
        return JSONResponse(await resp.json(), status_code=resp.status_code)
    async def relay():
        try:
            async for chunk in resp.aiter_raw():   # raw: no decoding, no buffering
                yield chunk
        finally:
            await resp.aclose()                    # cancels upstream on disconnect
    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
```

Non-negotiable: `aiter_raw`, no `json` middleware on `/v1`, no gzip middleware on
`/v1`, and Caddy configured with `flush_interval -1` for that path. A buffering
proxy anywhere in the chain turns streaming into a single blob at the end and is
the single most likely bug in this component.

### 5.4 `models.yaml`

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

`model_ref` replaces the former `hf_repo`: what it names depends on the backend —
an Ollama tag, a Hugging Face repo, or a provider's model string. `args` is passed
through only by backends that can use it. `estimated_load_seconds` is per-backend
and must be honest for the configuration in use, because the desktop app shows it
in the switch-confirmation dialog.

Validated with pydantic at startup; an invalid file is a hard startup failure with
a readable message, never a silent partial catalog.

### 5.5 Caddy

```caddyfile
harness.example.com {
    encode zstd gzip
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        -Server
    }
    @stream path /v1/*
    reverse_proxy @stream 127.0.0.1:8080 {
        flush_interval -1
        transport http { response_header_timeout 300s }
    }
    reverse_proxy 127.0.0.1:8080
    log { output file /var/log/caddy/harness.log }
}
```

`encode` must not apply to `/v1/*` — exclude it or streaming buffers. Verify with
`curl -N` during acceptance.

### 5.6 systemd

`harness-control.service`:

```ini
[Unit]
Description=LLM Harness control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=harness
EnvironmentFile=/etc/harness/harness.env
ExecStart=/opt/harness/.venv/bin/uvicorn harness_control.app:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5
KillMode=control-group        # takes the vLLM child down with it
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

vLLM is a **child of the control plane**, not its own unit. `KillMode=control-group`
guarantees no orphaned GPU process. (A systemd template unit per model is the
hardening upgrade — captured as ADR-0003, deliberately deferred.)

### 5.7 Configuration (`/etc/harness/harness.env`, mode 0600, owner `harness`)

| Var | Default | Notes |
|---|---|---|
| `HARNESS_API_KEY` | — | required; ≥ 32 random bytes base64url |
| `HARNESS_MODELS_FILE` | `/etc/harness/models.yaml` | |
| `HARNESS_VLLM_BIN` | `/opt/harness/.venv/bin/vllm` | |
| `HARNESS_VLLM_PORT` | `8000` | |
| `HARNESS_STATE_DIR` | `/var/lib/harness` | last_model, job history |
| `HARNESS_DRAIN_TIMEOUT_S` | `30` | |
| `HARNESS_LOAD_TIMEOUT_S` | `900` | |
| `HARNESS_AUTOLOAD_LAST` | `true` | |
| `HF_HOME` | `/mnt/models/hf` | weight cache on the data disk |
| `HF_TOKEN` | — | optional, for gated repos |
| `VLLM_LOGGING_LEVEL` | `INFO` | |

The key is never logged. A redaction filter on the logging config asserts this.

## 6. Provisioning

`scripts/provision.sh`, idempotent, run once on a fresh Ubuntu 24.04 GPU VM:

1. Verify `nvidia-smi` works; fail loudly if the driver is missing
2. Format/mount the data disk at `/mnt/models`, add to `/etc/fstab` by UUID
3. Create the `harness` system user; `/opt/harness`, `/etc/harness`, `/var/lib/harness`
4. Install Python 3.12, create `/opt/harness/.venv`, `pip install vllm` (pinned) + the control plane
5. Install Caddy, write the Caddyfile, `systemctl enable --now caddy`
6. Generate `HARNESS_API_KEY` if absent, write `harness.env` 0600, print the key **once**
7. `hf download` each model in the catalog into `HF_HOME` (this is the long step; do it here, not at first activation)
8. Install and enable `harness-control.service`
9. Run `scripts/smoke.sh` and report pass/fail

## 7. Acceptance criteria

Two lists, and only one of them is a gate. §7.1 is what the MVP is judged on,
because it is what the MVP runs on. §7.2 restates the same properties for the Azure
deployment and is **deferred by choice**, not outstanding work: there is no GPU
quota, so every B criterion is unrunnable rather than unmet. Nothing in §7.2 is a
capability §7.1 lacks — see `docs/BACKLOG.md`, *M4 (Azure, deferred)*.

### 7.1 Local target — the MVP gate

| # | Criterion |
|---|---|
| L1 | `scripts/dev-local.sh` starts Ollama and the control plane; `GET /healthz` returns 200 |
| L2 | Any `/v1` or `/admin` request without a valid bearer token returns 401 |
| L3 | `curl -N` on a streaming completion emits the first `data:` frame within 3 s and frames arrive incrementally (verify with `--trace-time`) |
| L4 | `POST /admin/models/qwen3.8-27b/activate` returns 202; `/admin/state` goes `loading` → `ready`; the switch completes within the advertised window |
| L5 | After a switch, `GET /api/ps` on the Ollama daemon shows **exactly one** loaded model |
| L6 | During a switch, `/v1/chat/completions` returns 503 `model_loading` with `Retry-After` — never a hang or a 500 |
| L7 | Requesting a model that is not active returns 409 `model_not_active` with the active id in `details` |
| L8 | Killing the Ollama daemon puts `/admin/state` into `error` within 5 s; the control plane stays up |
| L9 | Client disconnect mid-stream cancels the upstream request within 2 s |
| L10 | A concurrent second activation returns 409 `activation_in_progress` |
| L11 | `/admin/state.gpu` is `[]` and this is handled without error |
| L12 | An invalid `models.yaml` fails startup with a pydantic error naming the bad field |

L1–L11 are verified together, from a cold start, by `./scripts/smoke.sh --disruptive`
— one pass/fail line each, non-zero exit on any failure. L12 is a startup check
rather than a runtime one, and is covered by `tests/test_catalog.py`.

### 7.2 Azure GPU target — deferred, and unrunnable until quota exists

| # | Criterion |
|---|---|
| B1 | `provision.sh` on a fresh VM ends with `smoke.sh` green and no manual steps |
| B2 | `curl https://host/healthz` returns 200 with no auth header |
| B3 | Any `/v1` or `/admin` request without a valid bearer token returns 401 and logs nothing sensitive |
| B4 | `curl -N` on a streaming completion emits the first `data:` frame within 2 s and frames arrive incrementally (verify with `--trace-time`) |
| B5 | `POST /admin/models/qwen3.8-27b/activate` returns 202; `/admin/state` shows `loading` then `ready`; the whole switch completes under 180 s with weights pre-downloaded |
| B6 | During a switch, `/v1/chat/completions` returns 503 `model_loading` with `Retry-After` — never a hang or a 500 |
| B7 | Requesting a model that is not active returns 409 `model_not_active` with the active id in `details` |
| B8 | `kill -9` the vLLM process → `/admin/state` reports `error` within 5 s with the last log lines; the control plane stays up |
| B9 | `systemctl restart harness-control` leaves **zero** processes holding GPU memory (`nvidia-smi` shows 0 MB used before the reload) |
| B10 | VM reboot → the last active model is loaded and `ready` without a human |
| B11 | A concurrent second activation returns 409 `activation_in_progress` |
| B12 | Client disconnect mid-stream kills the upstream vLLM request within 2 s (visible in vLLM's aborted-request log line) |
| B13 | `nmap` from outside shows only 443 open; 8000 and 8080 are not reachable |
| B14 | An invalid `models.yaml` fails startup with a pydantic error naming the bad field |

These are held to the same standard as §7.1 when the hardware exists: run in one
pass, no manual nudges. They are not a promise that the code is ready — `vllm.py`
and `provision.sh` are written to spec and have never been executed.

## 8. Observability

MVP is deliberately small: structured JSON logs to the journal, plus the
`/admin/logs` ring buffer the desktop app reads. Every request logs
`{ts, method, path, status, duration_ms, model, request_id}` — no bodies, no key.
Prometheus/Grafana is post-MVP; vLLM's own `/metrics` stays bound to localhost.

## 9. Risks

| Risk | Mitigation |
|---|---|
| GPU memory not released after kill, so the next load OOMs | Kill the process *group*, then poll `nvidia-smi` for used-MB < 1000 before spawning; fail the activation with a clear error if it never drops (B9 covers this) |
| First activation stalls for 20 min downloading weights | Pre-download in `provision.sh`; `progress_hint` parses HF download output so the client shows real progress |
| Buffering somewhere in Caddy/uvicorn/httpx kills streaming | B4 tests it end to end with `curl -N --trace-time`; `flush_interval -1` and `aiter_raw` are called out in the spec |
| Running cost | Auto-shutdown schedule + `vm-stop.sh` + a README that leads with the hourly rate |
| vLLM version drift breaking CLI flags | Pin the exact vLLM version in `requirements.lock`; upgrades are a PR with a smoke run |
| Model repo names change upstream | Catalog is YAML with a startup validation that each repo resolves; a bad entry fails fast |
