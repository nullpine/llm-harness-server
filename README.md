# llm-harness-server

The server side of LLM Harness: a small FastAPI control plane that owns one vLLM
process at a time on an Azure GPU VM, behind Caddy with automatic TLS. The client
is [`llm-harness-desktop`](https://github.com/nullpine/llm-harness-desktop).

> ### 💸 This VM costs about **$7/hour** — roughly **$5,000/month** if you leave it on.
> `scripts/vm-stop.sh` deallocates it. Run it when you are done. Provisioning also
> configures an Azure auto-shutdown schedule. Treat this as part of the product,
> not an afterthought.

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

## Provisioning a VM

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
| `docs/DEPLOY.md` | provision, rotate the key, add a model, tear down |
| `docs/OPERATIONS.md` | troubleshooting: OOM, stuck load, orphan GPU process |
| `docs/adr/` | why vLLM, why one model at a time, why no crash-loop restart |
| `CLAUDE.md` | working instructions for AI contributors |

## Security posture (MVP)

Single shared API key, HTTPS only, NSG restricted to your IP, vLLM bound to
localhost. That is appropriate for one user and one VM and nothing more. Multi-user
identity via Entra ID is on the post-MVP list, not hacked in later as an
afterthought — see `docs/adr/0004-api-key-auth-for-mvp.md`.

## License

MIT
