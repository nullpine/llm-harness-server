# llm-harness-server

A small FastAPI **control plane** that keeps exactly one model serving at a time
and puts auth, model switching, and honest state in front of whatever is doing
the inference. The client is
[`llm-harness-desktop`](https://github.com/nullpine/llm-harness-desktop).

The engine is pluggable and the HTTP contract does not change when you swap it.
The same desktop build talks to Ollama on a laptop, vLLM on a rented GPU, or a
hosted provider — that interchangeability is the point of the whole design.

```bash
make profile          # which deployment am I pointed at?
make dev              # run it
./scripts/smoke.sh    # prove it works, L1-L11
```

---

## 1. Architecture

Four layers. Each owns one thing, and the boundaries are enforced by tests.

```
┌─ desktop app ──────────────────────────────────────────────────────────┐
│  holds conversation history; knows only the HTTP contract              │
└───────────────────────────┬────────────────────────────────────────────┘
                            │  Authorization: Bearer <HARNESS_API_KEY>
┌───────────────────────────▼──── control plane (this repo) ─────────────┐
│                                                                        │
│  auth.py         bearer token, constant-time compare, every route      │
│                  except GET /healthz                                   │
│                                                                        │
│  routes/         /v1/*        OpenAI-compatible surface                │
│                  /admin/*     state, models, jobs, logs                │
│                                                                        │
│  proxy.py        the streaming relay. Raw bytes in, raw bytes out.     │
│                  Never parses the stream. Cancels upstream on abort.   │
│                                                                        │
│  supervisor/     THE BRAIN. Owns the state machine, the activation     │
│                  lock, the drain, the job registry, the watchdog.      │
│                  Decides state; backends only report facts.            │
│                                                                        │
│  catalog.py      models.yaml → validated ModelSpec. Invalid catalog    │
│                  is a hard startup failure, never a partial load.      │
│                                                                        │
│  backends/       ollama │ vllm │ remote_openai                         │
│                  "how a model is made to serve", and nothing else      │
└───────────────────────────┬────────────────────────────────────────────┘
                            │  plain OpenAI HTTP, no auth on local paths
┌───────────────────────────▼────────────────────────────────────────────┐
│  the engine: ollama daemon │ our vLLM process │ someone else's HTTPS   │
└────────────────────────────────────────────────────────────────────────┘
```

### The supervisor owns state; backends never do

This split is the load-bearing idea. A backend answers "make this model serve",
"is it healthy", "is what you held released yet" — and nothing more. It never
sets state, never decides policy, never knows the state machine exists.

Everything else stays central, so it behaves identically on every backend: the
state machine, the activation lock (one switch at a time), the drain of in-flight
requests, the job registry, and the watchdog that notices a dead engine within
seconds.

The rule has teeth: `tests/test_backends.py` runs one shared contract suite
against *every* registered backend, and a test greps the source to fail the build
if `if backend.name == …` appears anywhere outside `backends/`. If a caller needs
to know which backend it has, the interface is missing a method — that is how
`upstream_headers()` came to exist rather than a branch in the proxy.

### The state machine

```
idle ──activate──▶ loading ──ready?──▶ ready ──activate──▶ stopping ──▶ loading
  ▲                   │                  │                                 │
  └───────────────────┴── failure ───────┴──▶ error ◀──────────────────────┘
```

`error` is terminal until a client activates something. There is no automatic
retry and no crash-loop restart — a model that fails to load will fail the same
way in three seconds, and a restart loop turns one clear error into a scrolling
log (ADR-0005).

### The three backends

| | `ollama` | `vllm` | `remote_openai` |
|---|---|---|---|
| Where | local daemon | our own process | someone else's HTTPS |
| activate | preload, pin with `keep_alive: -1` | spawn `vllm serve` as a process group | verify it appears in `/v1/models` |
| stop | `keep_alive: 0` | SIGTERM the **group**, then SIGKILL | nothing was acquired |
| released? | poll `/api/ps` until the tag is gone | poll `nvidia-smi` until VRAM returns | no-op |
| GPU info | `[]` (Apple Silicon) | `nvidia-smi` | `[]` — not our hardware |
| Status | the MVP path | implemented, **never run on a GPU** | verified against a real pod |

There are exactly three and there is no fourth. RunPod is not a backend — it is a
*deployment* of `remote_openai`, the same code path a hosted provider uses.

---

## 2. Deployment options

A **profile** is one deployment. Two halves, because the three differ in *work*,
not only in values:

- `deploy/profiles/<name>.env` — configuration, literal `KEY=value`, **no secrets**
- `scripts/profiles/<name>.sh` — the setup config cannot express (optional)

| Profile | Backend | Runs on | Cost | Status |
|---|---|---|---|---|
| `ollama` | `ollama` | this Mac, 48 GB | free | ✅ L1–L11 green |
| `runpod` | `remote_openai` | rented GPU pod | ~$3.52/hr | ✅ L1–L11 green |
| `vllm` | `vllm` | a CUDA host | varies | ⚠️ config only — nothing to run it on |

```bash
make profile              # show the active one and what exists
make profile runpod       # switch
make dev                  # run the active one
make dev PROFILE=ollama   # override for a single run
```

**Precedence**, strongest first: your shell environment → `.env.local` → the
profile file. So `HARNESS_PORT=8188 make dev` wins over both, and secrets live in
gitignored `.env.local` and never in a committed file.

Profiles stay literal with no shell expansion so the same file is valid as a
systemd `EnvironmentFile=` on a real host.

### `ollama` — local, free

```bash
brew install ollama
make profile ollama && make dev
```

Starts the daemon with `OLLAMA_MAX_LOADED_MODELS=1` — without it Ollama holds
three models at once and quietly breaks the one-model invariant on a machine with
enough RAM to hide it — pulls missing weights, generates an API key into
`.env.local`, and serves on `http://127.0.0.1:8080`.

| id | Ollama tag | Shape | Context |
|---|---|---|---|
| `glm-4.7-flash` | `glm-4.7-flash:q4_K_M` | 30B-A3B MoE, ~18 GB | 32k |
| `qwen3.8-27b` | `qwen3.8:27b-q4_K_M` | 27B dense, ~16 GB | 32k |

Both fit in 48 GB *at once* — which is exactly why the supervisor **verifies** the
old one unloaded rather than trusting that it did. With room to spare there is no
memory pressure to reveal a leak; the invariant would simply become quietly false.

### `runpod` — a rented GPU

The pod runs vLLM itself, so we do not own a process — hence `remote_openai`. The
control plane runs on your machine and proxies to the pod.

```
desktop ──▶ 127.0.0.1:8080 control plane ──HTTPS──▶ <pod>-8000.proxy.runpod.net
            HARNESS_API_KEY                          HARNESS_REMOTE_API_KEY
```

```bash
export RUNPOD_POD_ID=<pod id>          # or HARNESS_REMOTE_BASE_URL=https://…
# HARNESS_REMOTE_API_KEY=<the pod's --api-key>   ← put this in .env.local
make profile runpod && make dev
```

The setup hook asks the pod what it serves at `/v1/models` and generates
`.local/models.runpod.yaml` from the answer — `model_ref` must be exactly the name
the engine answers to, and the pod is the only authority on that. The catalog is
generated, never committed: it describes one pod serving one model, and both
change every deployment.

**Two keys, and they are not interchangeable.** `HARNESS_API_KEY` authenticates
the desktop app to us and stops at `auth.py`. `HARNESS_REMOTE_API_KEY`
authenticates *us* to the pod. Forwarding ours upstream would leak it.

**Limitation:** one pod serves one model, so switching between catalog entries is
instant and does nothing. Real switching needs a pod per model, or the control
plane running inside the pod on the `vllm` backend. Neither is built.

### `vllm` — our own process on a GPU host

Configuration only. The backend is real code — process-group spawn and kill, port
wait, VRAM wait, progress parsing — and passes the shared contract suite plus six
vllm-specific tests, with the fake upstream standing in for the binary. What no
test can reach is vLLM's own CLI accepting the flags, real weights loading, and
`wait_for_vram_release` watching VRAM genuinely come back.

The profile has no setup hook because the setup *is* provisioning, and
`scripts/provision.sh` is a deferred M4 stub. Intended shape:

```
Internet ──HTTPS:443──▶ Caddy ──▶ 127.0.0.1:8080 control plane ──▶ 127.0.0.1:8000 vLLM
```

Only 443 open; the engine never reachable from outside the host.

---

## 3. The client contract

`docs/API-CONTRACT.md` is authoritative and is **byte-identical** to the copy in
the desktop repo. Changing it means a PR in both repos and a version bump. This
section summarises; it does not replace it.

### Transport

| | |
|---|---|
| Auth | `Authorization: Bearer <key>` on every route except `GET /healthz` |
| Content type | `application/json`, or `text/event-stream` when streaming |
| Client identity | `X-Harness-Client: llm-harness-desktop/<semver>` (informational) |

### Every error uses one envelope

Never FastAPI's default `{"detail": …}`:

```json
{ "error": { "code": "model_not_active", "message": "human readable", "details": {} } }
```

Codes: `unauthorized`, `forbidden`, `not_found`, `model_not_active`,
`model_loading`, `activation_in_progress`, `activation_failed`,
`upstream_unavailable`, `bad_request`, `internal`.

### State decides what `/v1/chat/completions` does

| State | Meaning | Behaviour |
|---|---|---|
| `idle` | nothing loaded | 409 `model_not_active` |
| `loading` | weights loading | 503 `model_loading` + `Retry-After` |
| `ready` | serving | normal |
| `stopping` | draining before a switch | 503 `model_loading` + `Retry-After` |
| `error` | last activation failed | 409 `model_not_active`, see `last_error` |

It never hangs and never 500s while switching. `Retry-After` is the *incoming*
model's estimate, not the outgoing one — answering with the wrong one sends the
client back three times too early.

### Rules a client must follow

- **Correlate by your own request, never by the frame's `model` field.** Frames
  are relayed verbatim, so `model` carries the *engine's* name (`zai-org/GLM-4.7-Flash`,
  `glm-4.7-flash:q4_K_M`) rather than the catalog id you asked for. Rewriting it
  would mean parsing and re-serialising the stream, which is exactly the buffering
  the relay exists to prevent.
- **Tolerate both reasoning spellings** — `delta.reasoning_content` (vLLM) and
  `delta.reasoning` (Ollama).
- **Never auto-retry a stream that already emitted tokens.**
- Poll `/admin/state` every 2 s during a load, up to 15 min; 60 s idle timeout
  between SSE chunks; no total timeout on a completion — a long answer is not a
  hung one.

### Routes

| | |
|---|---|
| `GET /healthz` | unauthenticated; reveals no model or key info |
| `GET /v1/models` | the catalog, as OpenAI shapes it |
| `POST /v1/chat/completions` | streaming and non-streaming |
| `GET /admin/state` | state, active model, progress hint, last error, GPU |
| `GET /admin/models` | catalog + per-model availability |
| `POST /admin/models/{id}/activate` | 202 + job, or 409 if one is in flight |
| `GET /admin/jobs/{id}` | activation progress |
| `GET /admin/logs` | ring buffer, API key redacted |

---

## 4. Invariants that must not break

Each of these is defended in more than one place, because each fails *silently*.

**Exactly one model serves.** Every spawn and kill goes through
`backends/vllm.py`; no `subprocess.Popen` anywhere else in the package. Ollama is
pinned to one loaded model at the daemon, asserted at startup.

**Nothing buffers the stream.** `httpx` iterated with `aiter_raw()` — never
`aiter_text` or `.json()`; no compression middleware on `/v1/*`; Caddy
`flush_interval -1` where Caddy exists; and `tests/test_proxy_streaming.py`
asserts inter-chunk *arrival times*, not just content. A test that concatenates
the body and compares strings passes happily while streaming is completely broken.

**VRAM is actually released before the next load.** Process-group kill,
`KillMode=control-group` in the systemd unit, and `wait_for_vram_release()`
polling `nvidia-smi`. A vLLM process that survives a restart holds 30+ GB and the
next load OOMs.

> **Known gap.** SPEC §5.2 also requires killing any orphan holding the engine's
> port at startup. `kill_orphan_on_port()` in `backends/vllm.py` implements it,
> but nothing calls it — it has no callers and no tests. `docs/BACKLOG.md` tracks
> it under *M4*. It only bites on the `vllm` backend, which has no hardware yet.

**No secrets in the repo.** Keys live in gitignored `.env.local`; profiles carry
none. The logging filter scrubs the key from every record, with a test.

---

## 5. Development

The entire suite runs **without a GPU and without a daemon** —
`tests/fake_upstream.py` stands in for whatever is serving.

```bash
make test       # pytest
make lint       # ruff + ruff format --check + mypy --strict
make fmt        # ruff format + safe autofixes
make dev        # the active profile
make smoke HOST=http://127.0.0.1:8080
```

`./scripts/smoke.sh` exercises acceptance criteria L1–L11 against a *running*
deployment, one pass/fail line each, non-zero exit on failure. Criteria that
cannot apply are printed as loud SKIPs rather than silently passing — a
single-model deployment skips the four switch criteria.

---

## 6. Cost

The `ollama` profile costs nothing. The rest:

| | $/hr | 3 hr/day | Stopped |
|---|---:|---:|---:|
| RunPod H100 SXM 80GB | $3.52 | $317/mo | $0.024/hr storage |
| Azure H100 on-demand | $6.98 | $628/mo | — |
| Azure H100 spot | $1.29 | $116/mo | — |

Stopping a RunPod pod cuts the bill by 99.3% — the GPU is the entire cost and
storage is rounding error. It bills whether or not anything is talking to it, and
Ctrl-C on the control plane does **not** stop it.

One person chatting uses ~1–3% of an H100. The same models from a hosted API run
$2–20/month for heavy personal use. Self-host for tenant isolation, a pinned model
version, or the harness itself — not to save money (ADR-0006).

---

## 7. Security posture (MVP)

A single shared API key, one user, one client. Locally everything binds
`127.0.0.1` and nothing leaves the machine. On the RunPod path the pod is a public
HTTPS endpoint and its bearer token is the only thing in front of your GPU — set
one.

Explicitly **not** built, and deliberately so: per-user identity, quotas, and
usage tracking. Any key holder can also switch the model for everyone, since one
key opens both `/v1` and `/admin`. Multi-user via Entra ID is a post-MVP
*replacement*, not a layer to hack on later (ADR-0004).

---

## 8. Documentation

| | |
|---|---|
| `docs/SPEC.md` | the MVP spec, hardware plan, acceptance criteria |
| `docs/API-CONTRACT.md` | the HTTP surface — byte-identical copy in the desktop repo |
| `docs/BACKENDS.md` | the `Backend` interface, the three implementations, migration |
| `docs/PROJECT-STRUCTURE.md` | where things go |
| `docs/OPERATIONS.md` | the local runbook: startup refusals, stuck loads, a dead daemon |
| `docs/BACKLOG.md` | what is done, what is deferred, and why |
| `docs/adr/` | why one model at a time, why no crash-loop restart, why pluggable backends |
| `docs/DEPLOY.md` | *stub* — unwritten, because the provisioning is |
| `CLAUDE.md` | working instructions for AI contributors |

## License

MIT
