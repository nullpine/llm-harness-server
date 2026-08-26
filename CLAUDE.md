# CLAUDE.md — llm-harness-server

Instructions for Claude working in this repository. Read this before writing code.

## What this repo is

Everything that runs on the Azure GPU VM: a FastAPI control plane that owns one
vLLM process at a time, plus the provisioning, TLS, and systemd glue. The desktop
client lives in a **separate repo**, `llm-harness-desktop`.

## How to load context

This file loads automatically. **Nothing in `docs/` does.** Read them explicitly:

| Read | When |
|---|---|
| `docs/SPEC.md` | before any feature work — scope, hardware plan, acceptance criteria |
| `docs/API-CONTRACT.md` | before touching `routes/` or `proxy.py` |
| `docs/BACKENDS.md` | before touching `supervisor/`, `catalog.py`, or `models.yaml` |
| `docs/PROJECT-STRUCTURE.md` | before creating a new file — it says where things go |
| `docs/DEPLOY.md` / `docs/OPERATIONS.md` | before touching `scripts/` or `deploy/` |
| `docs/BACKLOG.md` | at the start of every session — find your current milestone |

`.claude/rules/` holds path-scoped rules that load automatically when you open the
files they cover. They are the short version; `docs/` is the long version.

**Start every session by reading `docs/BACKLOG.md` and stating which milestone and
item you are working on.**

## Ground rules

1. **The scope in `docs/SPEC.md` §3 is closed.** If a task appears to need
   something on the non-goals list, stop and say so instead of building it.
2. **Exactly one vLLM process may exist.** Every code path that spawns or kills it
   goes through `supervisor/process.py`. No `subprocess.Popen` anywhere else.
3. **Never edit `docs/API-CONTRACT.md` unilaterally.** It is byte-identical to the
   copy in the desktop repo. A change means a PR in both, and a version bump.
4. **Every error response uses the contract envelope**, never FastAPI's default
   `{"detail": ...}`.
5. **No secrets in the repo.** No API keys, hostnames, subscription ids, or
   `.env` files with real values. `deploy/config/harness.env.example` only.
6. `ruff` clean and `mypy --strict` clean before any PR.

## The two things most likely to go wrong

**Streaming buffering.** If any layer buffers, streaming silently degrades to one
blob at the end and everything still "works" in tests that only check the final
text. Defences, all required:

- `httpx` response iterated with `aiter_raw()`, never `aiter_text`/`.json()`
- no gzip/compression middleware on `/v1/*`
- Caddy `flush_interval -1` on `/v1/*`
- `tests/test_proxy_streaming.py` asserts inter-chunk arrival times, not just content

**Leaked GPU memory.** A vLLM process that survives a restart holds 30+ GB and the
next load OOMs. Defences, all required:

- spawn with `start_new_session=True`, kill the **process group**
- `systemd` unit uses `KillMode=control-group`
- `gpu.wait_for_vram_release()` polls `nvidia-smi` before spawning the next model
  and fails the activation with a clear error rather than launching into an OOM
- on control-plane startup, kill any orphan holding port 8000

## Working method

- Work one backlog item at a time; each is sized to a single PR.
- **The whole test suite runs without a GPU.** `tests/fake_vllm.py` stands in for
  vLLM. Nothing in `tests/` may import torch or require CUDA. If you cannot test
  something without a GPU, isolate the GPU-touching part behind a thin seam and
  test around it.
- Shell scripts must be idempotent and `shellcheck` clean. Someone will re-run
  `provision.sh` on a half-configured VM; it must not make things worse.
- If the spec is ambiguous or wrong, say so in the PR description and propose the
  fix rather than picking silently.

## Commands

| Command | Does |
|---|---|
| `make dev` | uvicorn with reload, pointed at `tests/fake_vllm.py` |
| `make test` | pytest, no GPU required |
| `make lint` | ruff check + ruff format --check + mypy |
| `make fmt` | ruff format |
| `make smoke HOST=https://…` | `scripts/smoke.sh` against a real deployment |

## Notes on vLLM

These apply to the **`vllm` backend only** — one of three, alongside `ollama` and
`remote_openai` (ADR-0007, `docs/BACKENDS.md`).

The MVP runs the `ollama` backend on local hardware. `vllm.py` is written to spec
but not exercised until GPU quota exists — do not let it rot, and do not let it
block local work.

- Pin the exact version in `requirements.lock`. CLI flags move between releases;
  an unpinned upgrade will break `models.yaml` args with no warning.
- Model-specific flags (`--tool-call-parser`, `--reasoning-parser`) belong in
  `models.yaml`, never hardcoded in Python.
- vLLM's `/metrics` and `/health` stay bound to `127.0.0.1`.
- Do not enable `--enable-sleep-mode` in MVP. It looks like the obvious way to make
  switching fast, but level-2 sleep is built for RLHF weight updates and adds a
  failure mode we do not need yet. Kill-and-respawn is boring and correct. This is
  ADR-0002; revisit it after the MVP ships.

## Cost discipline

This VM is roughly $7/hour. Anything you write that could leave it running —
a retry loop, a test that provisions, a doc that omits teardown — is a real bill.
`scripts/vm-stop.sh` is a first-class part of the product, and the README leads
with the hourly rate.
