# llm-harness-server

The server side of LLM Harness: a small FastAPI control plane that keeps exactly
one model serving at a time and puts auth, model switching, and honest state in
front of it. The client is
[`llm-harness-desktop`](https://github.com/nullpine/llm-harness-desktop).

The inference engine is pluggable. **Today it runs Ollama on an Apple Silicon Mac
— no cloud, no cost.** vLLM on an Azure H100 behind Caddy is the documented future
and is written to spec, waiting on GPU quota; it has never been run. The HTTP
contract is identical either way — see `docs/BACKENDS.md` and
`docs/adr/0007-pluggable-inference-backends.md`.

## Quick start

Requires an Apple Silicon Mac with **48 GB unified memory** — both catalog models
run at 4-bit — and [Ollama](https://ollama.com/download).

```bash
brew install ollama
./scripts/dev-local.sh
```

The script starts the Ollama daemon with `OLLAMA_MAX_LOADED_MODELS=1` (without it
Ollama holds three models at once and quietly breaks the one-model rule), pulls any
missing weights, generates an API key into `.env.local` on first run, and serves
the control plane on `http://127.0.0.1:8080`. It prints a ready-to-paste `curl -N`
streaming command. Re-running it is safe.

To check the whole thing actually works:

```bash
./scripts/smoke.sh                 # L1–L11, one pass/fail line each
./scripts/smoke.sh --disruptive    # also kills the daemon, to prove recovery
```

In the desktop app, set Server URL to `http://localhost:8080` and paste the key.
Nothing else differs from a cloud deployment.

```
desktop app ──HTTP──▶ 127.0.0.1:8080 control plane ──▶ 127.0.0.1:11434 ollama
                            │                                  │
                       models.yaml                     ~/.ollama/models
```

## The model catalog

| id | Ollama tag | Shape | Context |
|---|---|---|---|
| `glm-4.7-flash` | `glm-4.7-flash:q4_K_M` | 30B-A3B MoE, ~18 GB | 32k |
| `qwen3.8-27b` | `qwen3.8:27b-q4_K_M` | 27B dense, ~16 GB | 32k |

Both fit in 48 GB at once — which is exactly why the supervisor *verifies* the old
one unloaded before loading the next, rather than trusting that it did. Adding a
model is an entry in `deploy/config/models.yaml` plus a weight pull; no code change.

On a GPU these become the FP8 weights (`zai-org/GLM-4.7-Flash`,
`Qwen/Qwen3.8-27B-FP8`) with the same ids. The flagship GLM-5.x and Qwen3.8-Max
weights are 750B–2.4T parameters and need 8–32 GPUs; out of scope for this
hardware, see `docs/SPEC.md` §4.2.

## What it adds on top of the engine

Ollama and vLLM both already speak OpenAI. Neither authenticates clients, and
neither will tell you honestly what it is doing while it changes model. This
control plane does both:

- Bearer-token auth on every route, constant-time compared
- A supervisor that drains in-flight requests, stops the current model, **verifies
  the memory was actually released**, starts the next one, and reports honest state
  throughout — 503 with `Retry-After` while loading, never a hang
- A true streaming proxy — no buffering anywhere in the chain
- Enough operational surface (`/admin/state`, `/admin/logs`) that the desktop app
  can tell you *why* a load failed, without anyone opening a shell

## Development (no GPU required)

The entire test suite runs without a GPU and without a daemon — `tests/fake_upstream.py`
stands in for the engine.

```bash
make test       # pytest
make lint       # ruff + ruff format --check + mypy --strict
make fmt        # ruff format + safe autofixes
make dev        # scripts/dev-local.sh — the real local stack
make smoke HOST=http://127.0.0.1:8080
```

## The Azure GPU path — not built

There is no GPU quota, so this path has never existed — and the tooling for it is
not written. `scripts/provision.sh`, `vm-start.sh`, `vm-stop.sh` and
`rotate-key.sh` are **two-line stubs**; `docs/DEPLOY.md` is a stub; in `deploy/`
only the Caddyfile template has content. What *is* real is `supervisor/backends/vllm.py`,
written to spec against `docs/SPEC.md` §5 and covered by the shared backend
contract tests — it has simply never been run on a GPU.

`docs/BACKENDS.md` §4.1 is the migration when quota arrives; `docs/BACKLOG.md`
tracks the rest under *M4 (Azure, deferred)*. Deliberate scope, not unfinished
work — but do not mistake it for something you can provision today.

The intended shape:

```
Internet ──HTTPS:443──▶ Caddy ──▶ 127.0.0.1:8080 control plane ──▶ 127.0.0.1:8000 vLLM
```

Only 443 open; the engine never reachable from outside the VM.

> ### 💸 Before provisioning anything, the cost
>
> | | $/hr | 3 hr/day | 24/7 |
> |---|---:|---:|---:|
> | H100 on-demand | $6.98 | $628/mo | $5,095/mo |
> | **H100 spot** ← default | **$1.29** | **$116/mo** | $942/mo |
>
> None of this applies to the local path above, which costs nothing.
>
> One person chatting uses ~1–3 % of an H100. The same models from a hosted API run
> about $2–20/month for heavy personal use. Self-host when you need tenant
> isolation, a pinned model version, or the harness itself — not to save money. See
> `docs/adr/0006-spot-instances-and-cost.md`.
>
> When it is written, `provision.sh` will default to **Spot**, and `vm-stop.sh`
> will deallocate the VM. Until then the cost of this path is zero, because it
> cannot be started.

## Documentation

| | |
|---|---|
| `docs/SPEC.md` | the MVP spec, hardware plan, and acceptance criteria |
| `docs/API-CONTRACT.md` | the HTTP surface (byte-identical copy in the desktop repo) |
| `docs/BACKENDS.md` | the `Backend` interface, the three implementations, and how to migrate |
| `docs/OPERATIONS.md` | the local runbook: startup refusals, stuck loads, a daemon that died |
| `docs/BACKLOG.md` | what is done, what is deferred, and why |
| `docs/adr/` | why one model at a time, why no crash-loop restart, why pluggable backends |
| `docs/DEPLOY.md` | *stub* — the Azure runbook, unwritten because the path is unbuilt |
| `CLAUDE.md` | working instructions for AI contributors |

## Security posture (MVP)

A single shared API key, one user, one client. Locally everything is bound to
`127.0.0.1` and nothing leaves the machine; on the deployed path it would be HTTPS
only, the NSG restricted to one IP, and the engine bound to localhost. That is
appropriate for one person and nothing more. Multi-user identity via Entra ID is a
post-MVP *replacement*, not a layer to hack on later — see
`docs/adr/0004-api-key-auth-for-mvp.md`.

## License

MIT
