# LLM Harness — MVP Roadmap

Server milestones only. The desktop side of each milestone lives in
[llm-harness-desktop/docs/BACKLOG.md](https://github.com/nullpine/llm-harness-desktop/blob/main/docs/BACKLOG.md).
Milestone numbering is shared; contents are per-repo.

---

## M0 — Foundations (½ day, both repos)

Nothing runs yet; everything is in place to start.

- [ ] Create both GitHub repos, private, with the directory trees from
      `docs/PROJECT-STRUCTURE.md` (empty files with a one-line docstring are fine)
- [ ] Commit `SPEC.md`, `API-CONTRACT.md`, `PROJECT-STRUCTURE.md`, `CLAUDE.md`,
      `README.md` to each
- [ ] Toolchain: `package.json` / `pyproject.toml`, lint, format, typecheck, test
      runner — all wired and passing on an empty codebase
- [ ] CI green on both repos
- [ ] GitHub milestones M1–M5 created; the issues below filed against them

**Exit:** `npm run typecheck && npm run lint && npm run test` and
`make lint && make test` both pass on a repo with no features.

---

## M1 — Serve one model / stand up the shell (2–3 days)

### server
- [ ] `settings.py`, `errors.py`, `catalog.py` with `models.yaml` validation
- [ ] `auth.py` bearer dependency + `GET /healthz`
- [ ] `supervisor/state.py` state machine, exhaustively tested, no I/O
- [ ] `supervisor/backends/base.py` — the Backend Protocol + shared contract tests
- [ ] `supervisor/backends/ollama.py` — preload, keep_alive unload, `/api/ps` health
- [ ] `supervisor/backends/vllm.py` — process group spawn/kill, VRAM release (written to spec, not exercised until GPU quota)
- [ ] `supervisor/backends/remote.py` — no-op activate/stop, `/v1/models` health
- [ ] `routes/openai.py` + `proxy.py` streaming relay against `fake_vllm`
- [ ] `GET /v1/models`, `GET /admin/state`
- [ ] `tests/fake_vllm.py` + `test_proxy_streaming.py` (asserts incremental arrival)
- [ ] `scripts/provision.sh` up to "one model serving over HTTPS"
- [ ] **First real deploy.** GLM 4.7 Flash answering `curl -N` through Caddy.

**Exit (server):** L1, L2, L3 (see SPEC §7.1).

---

## M2 — Chat, and switch models (3–4 days)

### server
- [x] `supervisor/supervisor.py`: activate, drain, activation lock, job registry
- [x] `POST /admin/models/{id}/activate`, `GET /admin/jobs/{id}`, `GET /admin/models`
- [x] Watchdog: backend death → `error` within 5 s (measured: 1.5 s)
- [x] `logbuf.py` + `GET /admin/logs`
- [x] Log an aborted relay at INFO, not DEBUG — a client disconnect now leaves a
      line at the default level, so A3/L9 can be confirmed by reading logs.
- [ ] `progress_hint` parsed from vLLM/HF output during load
- [ ] `scripts/download-models.sh`; both models pre-downloaded on the VM

**Exit (server):** L4–L11.

---

## M3 — The dropdown (1–2 days)

Entirely desktop. Nothing on the server moves; see the desktop backlog.

---

## M4 — Harden (2 days)

- [ ] `HARNESS_AUTOLOAD_LAST` — reboot restores the last model
- [ ] Orphan-process cleanup on startup; `wait_for_vram_release` before every spawn
- [ ] `logging_config.py` redaction + `test_redaction.py` in both repos
- [ ] NSG locked to 443 from your IP; verify with `nmap`
- [ ] `scripts/rotate-key.sh`, `vm-start.sh`, `vm-stop.sh`, Azure auto-shutdown
- [ ] Spot provisioning (`--priority Spot --eviction-policy Deallocate`); verify
      the app recovers cleanly from an eviction and `vm-start.sh` reports capacity
      errors clearly (ADR-0006)
- [ ] `scripts/smoke.sh` covering the full B-list
- [ ] `docs/OPERATIONS.md`: OOM, stuck load, orphan GPU process, expired cert
- [ ] Azure GPU path: `provision.sh`, Caddy, systemd, spot provisioning — gated on
      pay-as-you-go + `NCADS_H100_v5` standard **and** spot quota. Verified against
      SPEC §7.2 when hardware exists

**Exit:** B1, B8, B9, B10, B13, B14.

---

## M5 — Ship (1 day)

- [ ] `electron-builder` dmg that launches on a clean macOS machine
- [ ] End-to-end run of every acceptance criterion, A1–A12 and B1–B14, recorded
- [ ] READMEs finished: setup from zero, cost warning, teardown
- [ ] ADRs written for the decisions actually made
- [ ] Tag `v0.1.0` in both repos
- [ ] Post-MVP backlog groomed from everything deferred along the way

**Exit:** you can hand someone the dmg and the provision script and they get a
working private LLM.

---

## Deferred — the post-MVP list

Kept here so it stays out of the MVP. Roughly in the order it will matter.

1. Token counting and context-aware history trimming
2. SQLite persistence behind the existing repository interface
3. Conversation search and export
4. Two models resident on a multi-GPU VM; per-conversation model pinning
5. vLLM sleep mode for sub-10-second switching (ADR-0002 revisit)
6. Tool calling / MCP
7. Vision input (Qwen 3.8 27B already supports it)
8. Prometheus + Grafana; vLLM metrics scraping
9. Entra ID auth, multi-user
10. Bicep/Terraform for the VM; a systemd template unit per model
11. Auto-update for the desktop app; code signing and notarization
12. Scale-to-zero: deallocate the VM on idle, start it from the app
13. A hosted OpenAI-compatible provider as a second catalog entry — the desktop app
    already speaks the contract, so pointing it at a hosted endpoint is a config
    change. Useful as the everyday default with the VM reserved for private work.
14. Real cross-repo contract check. Today each repo verifies its own
    API-CONTRACT.md against its own stamp, which catches a local edit that skipped
    re-stamping but not divergence between the repos. A CI step fetching the other
    repo's `.api-contract.sha256` and comparing would close it.
15. Derive the contract instead of duplicating it. FastAPI emits OpenAPI from the
    route definitions; publish that and generate the desktop's types from it. Replaces
    the byte-identical API-CONTRACT.md copies and their hash stamps — drift becomes
    impossible rather than merely detected, and a field rename becomes one PR.
