# LLM Harness — MVP Roadmap

Two repos, six milestones. The server leads by one milestone so the desktop app
always has something real to build against — but the desktop app is unblocked from
day one by its mock server, so M1 and M2 can run in parallel.

```
        M0 ──────────────────────────────────────────────────────► both repos
        │
server  ├── M1 serve one model ──► M2 switch models ──► M4 harden ──┐
        │                                                            ├──► M5 ship
desktop └── M1 mock + shell ────► M2 chat + stream ──► M3 dropdown ──┘
```

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
- [ ] `supervisor/process.py` spawn/kill a process group; `gpu.py` VRAM polling
- [ ] `routes/openai.py` + `proxy.py` streaming relay against `fake_vllm`
- [ ] `GET /v1/models`, `GET /admin/state`
- [ ] `tests/fake_vllm.py` + `test_proxy_streaming.py` (asserts incremental arrival)
- [ ] `scripts/provision.sh` up to "one model serving over HTTPS"
- [ ] **First real deploy.** GLM 4.7 Flash answering `curl -N` through Caddy.

### desktop
- [ ] `src/shared/types.ts`, `ipc.ts`, `constants.ts`
- [ ] `scripts/dev-mock-server.mjs` implementing the full contract, incl. fake loads
- [ ] `harnessClient.ts` + `sseStream.ts`, unit-tested against the mock
- [ ] `settingsStore.ts` + `secretStore.ts`
- [ ] Window with security hardening, preload bridge, empty React shell
- [ ] Settings modal with Test connection working end to end

**Exit (server):** B2, B3, B4 from `SPEC.md` §7.
**Exit (desktop):** A1, A8, A9 from `SPEC.md` §10.

---

## M2 — Chat, and switch models (3–4 days)

### server
- [ ] `supervisor/supervisor.py`: activate, drain, activation lock, job registry
- [ ] `POST /admin/models/{id}/activate`, `GET /admin/jobs/{id}`, `GET /admin/models`
- [ ] Watchdog: vLLM death → `error` within 5 s
- [ ] `logbuf.py` + `GET /admin/logs`
- [ ] `progress_hint` parsed from vLLM/HF output during load
- [ ] `scripts/download-models.sh`; both models pre-downloaded on the VM

### desktop
- [ ] `conversationStore.ts` with atomic writes and corrupt-file tolerance
- [ ] Chat pane: message list, bubbles, markdown, code copy, streaming cursor
- [ ] Composer: Enter/Shift+Enter, autogrow, Stop button with real abort
- [ ] `serverPoller.ts` adaptive polling → `models:stateChanged`
- [ ] Conversation sidebar: create, list, rename, delete, date grouping
- [ ] Reasoning block (collapsed `Thinking`)

**Exit (server):** B5, B6, B7, B11, B12.
**Exit (desktop):** A2, A3, A7, A10, A12.

---

## M3 — The dropdown (1–2 days, desktop)

- [ ] `ModelDropdown` + `ModelStatusPill` fed by the catalog
- [ ] `SwitchModelDialog` with the honest load-time warning
- [ ] `LoadingBanner` with elapsed time and `progressHint`; composer disabled
- [ ] Failure path: red banner, `lastError`, **View server logs** modal
- [ ] `— switched to X —` divider in the transcript; per-message `modelId` label
- [ ] `unreachable` state handling and automatic recovery

**Exit:** A4, A5, A6.

---

## M4 — Harden (2 days, mostly server)

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
- [ ] Desktop: idle-chunk timeout, retry on failed message, error envelope → friendly copy

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
