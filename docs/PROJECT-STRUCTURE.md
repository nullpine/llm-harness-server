# llm-harness-server — Repository Structure

```
llm-harness-server/
├── .claude/                           # ← how Claude Code picks this project up
│   ├── rules/                         # path-scoped; load when matching files are opened
│   │   ├── streaming.md               # the four anti-buffering defences
│   │   ├── process-safety.md          # one vLLM, kill the group, free the VRAM
│   │   └── backend-boundary.md        # what belongs in a Backend, what in the supervisor
│   ├── settings.json                  # committed: allowed/denied tool permissions
│   └── settings.local.json            # gitignored: personal overrides
│
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug.yml
│   │   └── task.yml
│   ├── pull_request_template.md
│   └── workflows/
│       ├── ci.yml                     # ruff → mypy → pytest (no GPU needed; vLLM is mocked)
│       └── shellcheck.yml             # lint every script in scripts/
│
├── docs/
│   ├── SPEC.md                        # ← the MVP spec
│   ├── API-CONTRACT.md                # ← identical copy of the shared contract
│   ├── BACKENDS.md                    # the Backend interface, the three impls, migration
│   ├── DEPLOY.md                      # runbook: provision, rotate key, add a model, tear down
│   ├── OPERATIONS.md                  # troubleshooting: OOM, stuck load, orphan GPU process
│   ├── BACKLOG.md
│   └── adr/
│       ├── 0001-vllm-over-ollama.md
│       ├── 0002-single-model-supervisor.md
│       ├── 0003-subprocess-not-systemd-template.md
│       ├── 0004-api-key-auth-for-mvp.md
│       ├── 0005-no-crash-loop-restart.md
│       ├── 0006-spot-instances-and-cost.md
│       └── 0007-pluggable-inference-backends.md
│
├── src/
│   └── harness_control/
│       ├── __init__.py
│       ├── __main__.py                # python -m harness_control (dev entrypoint)
│       ├── app.py                     # FastAPI factory + lifespan
│       ├── settings.py                # pydantic-settings, all HARNESS_* env vars
│       ├── auth.py                    # bearer dependency, constant-time compare
│       ├── errors.py                  # ErrorCode enum + exception → contract envelope
│       ├── models.py                  # pydantic schemas for every request/response body
│       ├── catalog.py                 # models.yaml loader + ModelSpec + validation
│       ├── supervisor/
│       │   ├── __init__.py
│       │   ├── state.py               # the state enum + transition guards
│       │   ├── supervisor.py          # activate(), watchdog, activation lock
│       │   ├── jobs.py                # in-memory job registry (+ last 20 on disk)
│       │   └── backends/
│       │       ├── __init__.py        # registry: name → Backend impl
│       │       ├── base.py            # the Backend Protocol + ResourceInfo
│       │       ├── ollama.py          # local Apple Silicon — the MVP path
│       │       ├── vllm.py            # process spawn/kill, readiness, VRAM release
│       │       └── remote.py          # remote_openai — hosted or our own remote VM
│       ├── routes/
│       │   ├── __init__.py
│       │   ├── health.py              # GET /healthz  (unauthenticated)
│       │   ├── openai.py              # GET /v1/models, POST /v1/chat/completions
│       │   └── admin.py               # /admin/models, /admin/state, /admin/jobs, /admin/logs
│       ├── proxy.py                   # streaming relay, abort propagation
│       ├── logbuf.py                  # bounded ring buffer fed by the vLLM pipes
│       └── logging_config.py          # JSON logs + API-key redaction filter
│
├── tests/
│   ├── conftest.py                    # app fixture with a FakeSupervisor
│   ├── fake_upstream.py               # uvicorn stub for any backend: /health, /v1/*, SSE frames
│   ├── test_auth.py
│   ├── test_catalog.py
│   ├── test_state_machine.py          # every legal + illegal transition
│   ├── test_backends.py               # the shared contract suite, run against every backend
│   ├── test_supervisor_activate.py    # drain, timeout, concurrent activate → 409
│   ├── test_proxy_streaming.py        # asserts chunks arrive incrementally, not batched
│   ├── test_proxy_abort.py            # client disconnect cancels upstream
│   ├── test_proxy_upstream_auth.py    # the relay presents the upstream's key, not ours
│   ├── test_admin_routes.py
│   ├── test_errors_contract.py        # every error code matches the contract table
│   └── test_redaction.py              # the API key never reaches a log record
│
├── deploy/
│   ├── caddy/
│   │   └── Caddyfile.template
│   ├── systemd/
│   │   └── harness-control.service
│   ├── profiles/                      # one deployment each: values only, no secrets
│   │   ├── ollama.env
│   │   ├── runpod.env
│   │   └── vllm.env
│   ├── config/
│   │   ├── models.yaml                # the MVP catalog (GLM 4.7 Flash + Qwen 3.8 27B)
│   │   └── harness.env.example
│   └── logrotate/
│       └── harness
│
├── scripts/
│   ├── dev.sh                         # ← the entry point: runs the active deployment profile
│   ├── profile.sh                     # show or switch the active profile
│   ├── profiles/                      # per-profile setup: what config alone cannot express
│   │   ├── ollama.sh                  # start the daemon, pull the tags
│   │   └── runpod.sh                  # probe the pod, generate its catalog
│   ├── dev-local.sh                   # thin shim → dev.sh ollama
│   ├── dev-runpod.sh                  # thin shim → dev.sh runpod
│   ├── provision.sh                   # one-shot fresh-VM setup (idempotent)
│   ├── install-nvidia.sh              # driver + CUDA, skipped if nvidia-smi already works
│   ├── mount-data-disk.sh             # format + fstab by UUID → /mnt/models
│   ├── download-models.sh             # hf download every repo in models.yaml
│   ├── rotate-key.sh                  # generate + install a new API key
│   ├── smoke.sh                       # curl-based end-to-end check (health, auth, stream, switch)
│   ├── vm-start.sh                    # az vm start
│   ├── vm-stop.sh                     # az vm deallocate  ← the money saver
│   └── tail-logs.sh
│
├── .dockerignore
├── .env.example
├── .gitignore
├── .python-version                    # 3.12
├── CHANGELOG.md
├── CLAUDE.md                          # ← instructions for Claude working in this repo
├── LICENSE
├── Makefile                           # dev, test, lint, fmt, smoke
├── README.md
├── pyproject.toml                     # ruff + mypy + pytest config, project metadata
└── requirements.lock                  # pinned, incl. the exact vLLM version
```

## Build order

1. `settings.py`, `errors.py`, `models.py`, `catalog.py` — config and vocabulary, fully tested
2. `supervisor/state.py` + `test_state_machine.py` — the state machine on its own, no I/O
3. `supervisor/backends/base.py` — the `Backend` Protocol and the shared contract tests
4. `supervisor/backends/ollama.py` — the MVP path; preload, `keep_alive: 0` unload, `/api/ps` health
5. `supervisor/backends/vllm.py` (spawn/kill the process group, readiness polling,
   `wait_for_vram_release()`) and `backends/remote.py`. `vllm.py` satisfies the shared
   contract suite like any other backend; what it has **not** met is a GPU — vLLM's
   own CLI, real weights, and VRAM actually being released. It must stay lint- and
   type-clean, and it must not gate local work
6. `supervisor/supervisor.py` — wire it together
7. `routes/` + `proxy.py` — HTTP surface; `test_proxy_streaming.py` is the one that catches buffering
8. `deploy/` + `scripts/` — provisioning last, once there is something to provision

`tests/fake_upstream.py` means the entire test suite runs on GitHub Actions with
no GPU and no daemon. Nothing in `tests/` may require CUDA.

## Conventions

- `ruff` (lint + format) and `mypy --strict` both clean; CI fails otherwise
- Every route returns the contract's error envelope — never FastAPI's default
  `{"detail": ...}`. A custom exception handler enforces this, and
  `test_errors_contract.py` asserts it for every code.
- No blocking calls in async paths: `nvidia-smi` and process waits go through
  `asyncio.create_subprocess_exec` / `run_in_executor`
- Shell scripts: `set -euo pipefail`, `shellcheck` clean, safe to re-run
- Secrets come from the environment only. No key, token, or hostname is committed.
