# LLM Harness — MVP Roadmap

Server milestones only. The desktop side of each milestone lives in
[llm-harness-desktop/docs/BACKLOG.md](https://github.com/nullpine/llm-harness-desktop/blob/main/docs/BACKLOG.md).
Milestone numbering is shared; contents are per-repo.

---

## M0 — Foundations (½ day, both repos)

Nothing runs yet; everything is in place to start.

- [x] Create both GitHub repos, private, with the directory trees from
      `docs/PROJECT-STRUCTURE.md` (empty files with a one-line docstring are fine)
- [x] Commit `SPEC.md`, `API-CONTRACT.md`, `PROJECT-STRUCTURE.md`, `CLAUDE.md`,
      `README.md` to each
- [x] Toolchain: `package.json` / `pyproject.toml`, lint, format, typecheck, test
      runner — all wired and passing on an empty codebase
- [x] CI green on both repos
- [ ] GitHub milestones M1–M5 created; the issues below filed against them
      *(not done — the work was tracked in this file and in PRs instead)*

**Exit:** `npm run typecheck && npm run lint && npm run test` and
`make lint && make test` both pass on a repo with no features.

---

## M1 — Serve one model / stand up the shell (2–3 days)

### server
- [x] `settings.py`, `errors.py`, `catalog.py` with `models.yaml` validation
- [x] `auth.py` bearer dependency + `GET /healthz`
- [x] `supervisor/state.py` state machine, exhaustively tested, no I/O
- [x] `supervisor/backends/base.py` — the Backend Protocol + shared contract tests
- [x] `supervisor/backends/ollama.py` — preload, keep_alive unload, `/api/ps` health
- [x] `supervisor/backends/vllm.py` — process group spawn/kill, VRAM release (written to spec, not exercised until GPU quota)
- [x] `supervisor/backends/remote.py` — no-op activate/stop, `/v1/models` health
- [x] `routes/openai.py` + `proxy.py` streaming relay against a fake upstream
- [x] `GET /v1/models`, `GET /admin/state`
- [x] `tests/fake_upstream.py` + `test_proxy_streaming.py` (asserts incremental
      arrival). Named for the upstream, not for vLLM: with three backends there is
      no single "the engine" to fake
- [ ] `scripts/provision.sh` up to "one model serving over HTTPS" — **deferred to
      M4 (Azure)**, blocked on GPU quota
- [ ] **First real deploy.** GLM 4.7 Flash answering `curl -N` through Caddy —
      **deferred to M4 (Azure)**. The local equivalent (L1–L3 through
      `dev-local.sh`) is green

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
- [ ] `progress_hint` parsed from vLLM/HF output during load — **deferred to M4
      (Azure)**. On the ollama path the hint is the phase name, which is what
      `/admin/state` and the app show today
- [ ] `scripts/download-models.sh`; both models pre-downloaded on the VM —
      **deferred to M4 (Azure)**. `dev-local.sh` pulls the ollama tags

**Exit (server):** L4–L11.

---

## M3 — The dropdown (1–2 days)

Entirely desktop. Nothing on the server moves; see the desktop backlog.

---

## M4 — Harden

Split, because the two halves are not the same kind of work: one runs on the
machine in front of you, the other is written against a VM that does not exist.

### M4 (local) — done

- [x] Control-plane lifecycle logging in `/admin/logs`: every activation, drain,
      unload and failure, with elapsed ms. Previously the buffer was fed only by
      the vLLM stdout pump, so on the Ollama path the desktop app's "View server
      logs" — offered exactly when an activation fails — had nothing to show
- [x] `logging_config.py` redaction + `test_redaction.py`, covering the ring
      buffer: the API key appears in no record
- [x] `scripts/smoke.sh` — L1–L11 against a running deployment, pass/fail per
      criterion, non-zero exit on failure. L3 by frame arrival times, L5 by
      `/api/ps` during *and* after a switch, L6, L10. L8 behind `--disruptive`
- [x] `docs/OPERATIONS.md` — the local runbook
- [x] The aborted-request log in `proxy.py` at INFO rather than DEBUG (done in M2)

### M4 (Azure, deferred — blocked on GPU quota)

**None of this is a gap in the MVP.** The MVP ships on the local Ollama path
(SPEC §4.3); every item here belongs to a deployment that has no hardware yet,
gated on a pay-as-you-go subscription plus `NCADS_H100_v5` standard **and** spot
quota. Deliberate scope, not unfinished work. `docs/BACKENDS.md` §4.1 is the
migration when quota arrives.

- [ ] `scripts/provision.sh` (a 2-line stub today), Caddy (TLS, `flush_interval -1`
      on `/v1/*` — `deploy/caddy/Caddyfile.template` is the one deploy file with
      real content), and filling in the empty placeholders:
      `deploy/systemd/harness-control.service` (the text is in SPEC §5.6),
      `deploy/logrotate/harness`, `deploy/config/harness.env.example`
- [ ] Spot provisioning (`--priority Spot --eviction-policy Deallocate`); the app
      recovers cleanly from an eviction and `vm-start.sh` reports capacity errors
      clearly (ADR-0006)
- [ ] `scripts/rotate-key.sh`, `vm-start.sh`, `vm-stop.sh`, Azure auto-shutdown
- [ ] NSG locked to 443 from your IP; verify with `nmap`
- [ ] `HARNESS_AUTOLOAD_LAST` — reboot restores the last model
- [ ] Orphan-process cleanup on startup; `wait_for_vram_release` before every
      spawn (vLLM only — the Ollama backend owns no process and no VRAM)
- [ ] `docs/OPERATIONS.md`: the Azure half — OOM, orphan GPU process, expired
      cert. Written against a real VM, not guessed
- [ ] Verify SPEC §7.2 (the B-list) end to end when hardware exists

**Exit (local):** L1–L11 green under `scripts/smoke.sh`; a failed activation is
diagnosable from `/admin/logs` alone.
**Exit (Azure):** deferred with the rest of the group.

---

## M5 — Ship (1 day)

- [ ] `electron-builder` dmg that launches on a clean macOS machine *(desktop repo)*
- [x] End-to-end run of every acceptance criterion, recorded — **L1–L11 green in one
      cold-start `./scripts/smoke.sh --disruptive`**, pasted into the release PR.
      B1–B14 are unrunnable (no GPU quota); A1–A12 are the desktop repo's
- [x] README finished: setup from zero, the cost warning where it belongs, and an
      Azure section that says the path is unbuilt rather than implying otherwise
- [x] ADRs written for the decisions actually made — 0001–0005 were empty templates
      marked *proposed*; they now carry the reasoning and are *accepted*
- [ ] Tag `v0.1.0` in both repos
- [x] Post-MVP backlog groomed from everything deferred along the way — see the
      Deferred list, and *M4 (Azure, deferred)* above

**Exit (this repo):** `make lint && make test` clean with no daemon running, and
L1–L11 green in one run of `scripts/smoke.sh --disruptive` from a cold start.

**Exit (the original, both repos):** you can hand someone the dmg and the provision
script and they get a working private LLM. Half of that stands: the dmg plus
`dev-local.sh` gives you a working private LLM on a Mac. The provision script is
deferred with the rest of the Azure path.

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
