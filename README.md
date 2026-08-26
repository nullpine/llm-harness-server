# llm-harness-server

The server side of LLM Harness: a small FastAPI control plane that keeps exactly
one model serving at a time and puts auth, model switching, and honest state in
front of it. The client is
[`llm-harness-desktop`](https://github.com/nullpine/llm-harness-desktop).

The inference engine is pluggable. **Today it runs Ollama on an Apple Silicon Mac
— no cloud, no cost.** vLLM on an Azure H100 behind Caddy is the production target
and is written to spec, waiting on GPU quota. The HTTP contract is identical either
way; see `docs/BACKENDS.md` and `docs/adr/0007-pluggable-inference-backends.md`.

## Running locally (no Azure)

The MVP target. Requires a Mac with **32 GB unified memory** — both catalog models
run at 4-bit — and [Ollama](https://ollama.com/download).

```bash
brew install ollama
./scripts/dev-local.sh
```

The script starts the Ollama daemon with `OLLAMA_MAX_LOADED_MODELS=1` (without it
Ollama holds three models at once and quietly breaks the one-model rule), pulls any
missing weights, generates an API key into `.env` on first run, and serves the
control plane on `http://127.0.0.1:8080`. It prints a ready-to-paste `curl -N`
streaming smoke command. Re-running it is safe.

In the desktop app, set Server URL to `http://localhost:8080` and paste the key.
Nothing else differs from a cloud deployment.

Moving to a GPU later is a deployment change plus a catalog edit, not a code
change — `docs/BACKENDS.md` §4 has the steps.

> ### 💸 Azure GPU target — read this before provisioning anything
>
> | | $/hr | 3 hr/day | 24/7 |
> |---|---:|---:|---:|
> | H100 on-demand | $6.98 | $628/mo | $5,095/mo |
> | **H100 spot** ← default | **$1.29** | **$116/mo** | $942/mo |
>
> None of this applies to the local Ollama path above, which costs nothing.
>
> `provision.sh` uses **Spot** by default. `scripts/vm-stop.sh` deallocates the VM;
> run it when you are done, and provisioning also sets an auto-shutdown schedule.
>
> Also worth knowing up front: one person chatting uses ~1–3 % of an H100. The same
> models from a hosted API run about $2–20/month for heavy personal use. Self-host
> when you need tenant isolation, a pinned model version, or the harness itself —
> not to save money. See `docs/adr/0006-spot-instances-and-cost.md`.

## What it adds on top of vLLM

vLLM already speaks OpenAI. It does not authenticate clients, and it cannot change
which model is loaded. This control plane does both:

- Bearer-token auth on every route, constant-time compared
- A supervisor that stops the current model, waits for VRAM to actually free, starts
  the next one, and reports honest state throughout
- A true streaming proxy — no buffering anywhere in the chain
- Enough operational surface (`/admin/state`, `/admin/logs`) that the desktop app
  can tell you *why* a load failed

## Architecture

```
Internet ──HTTPS:443──▶ Caddy ──▶ 127.0.0.1:8080 control plane ──▶ 127.0.0.1:8000 vLLM
                                        │                              │
                                   models.yaml                  /mnt/models (weights)
```

Only 443 is open. vLLM is never reachable from outside the VM.

## MVP model catalog

| id | Model | Shape | Notes |
|---|---|---|---|
| `glm-4.7-flash` | `zai-org/GLM-4.7-Flash` | 30B-A3B MoE, FP8 | fast, strong at coding |
| `qwen3.8-27b` | `Qwen/Qwen3.8-27B-FP8` | 27B dense, FP8 | 262k context capable; MVP caps at 32k |

Both fit one H100 NVL (94 GB), one at a time. Adding a model is an entry in
`deploy/config/models.yaml` plus a weight download — no code change.

The flagship GLM-5.x and Qwen3.8-Max weights are 750B–2.4T parameters and need
8–32 GPUs. They are out of scope for this hardware; see `docs/SPEC.md` §4.2.

## Provisioning a VM (the Azure GPU target)

```bash
# on a fresh Ubuntu 24.04 GPU VM (Standard_NC40ads_H100_v5)
git clone https://github.com/nullpine/llm-harness-server /opt/harness/src
cd /opt/harness/src
sudo ./scripts/provision.sh --host harness.example.com
```

The script is idempotent and safe to re-run. It ends by printing your API key
**once** and running `scripts/smoke.sh`. Full runbook: `docs/DEPLOY.md`.

## Development (no GPU required)

The entire test suite runs against `tests/fake_vllm.py`.

```bash
make dev        # uvicorn --reload against the fake vLLM
make test       # pytest
make lint       # ruff + mypy --strict
make smoke HOST=https://harness.example.com
```

## Documentation

| | |
|---|---|
| `docs/SPEC.md` | the MVP spec, hardware plan, and acceptance criteria |
| `docs/API-CONTRACT.md` | the HTTP surface (identical copy in the desktop repo) |
| `docs/BACKENDS.md` | the `Backend` interface, the three implementations, and how to migrate |
| `docs/DEPLOY.md` | provision, rotate the key, add a model, tear down |
| `docs/OPERATIONS.md` | troubleshooting: OOM, stuck load, orphan GPU process |
| `docs/adr/` | why one model at a time, why no crash-loop restart, why pluggable backends |
| `CLAUDE.md` | working instructions for AI contributors |

## Security posture (MVP)

Single shared API key, HTTPS only, NSG restricted to your IP, vLLM bound to
localhost. That is appropriate for one user and one VM and nothing more. Multi-user
identity via Entra ID is on the post-MVP list, not hacked in later as an
afterthought — see `docs/adr/0004-api-key-auth-for-mvp.md`.

## License

MIT
