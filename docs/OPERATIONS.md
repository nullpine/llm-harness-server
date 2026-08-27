# Operations & troubleshooting

The runbook for the **local path** — an Ollama daemon and the control plane on one
machine, started by `scripts/dev-local.sh` (SPEC §4.3). The Azure path is not built;
see the last section before following anything written for it.

Two things to reach for before anything else:

    ./scripts/smoke.sh                                    # L1–L11, pass/fail per criterion
    curl -H "Authorization: Bearer $KEY" \
      'http://127.0.0.1:8080/admin/logs?lines=200&source=control'

`/admin/logs` is the control plane's own lifecycle, not the model's: every
activation, drain, unload, and failure, with elapsed milliseconds. The desktop app
shows the same buffer under **View server logs**, which is what someone without
shell access will be reading. The API key is scrubbed from it.

---

## The control plane refuses to start

    OLLAMA_MAX_LOADED_MODELS is not set. Ollama defaults to 3 concurrent models…
    OLLAMA_MAX_LOADED_MODELS is '3', must be 1…

This is deliberate and fatal, not a warning. Ollama's default lets a switch leave
the old model resident beside the new one while `/admin/state` reports one active,
and this machine has 48 GB — there is no memory pressure to reveal it. The
invariant just becomes quietly false.

    ./scripts/dev-local.sh          # starts the daemon with the right value

If a daemon is **already listening**, `dev-local.sh` reuses it and cannot vouch for
its environment — it says so. Ollama.app supervises its own daemon and will respawn
it, so `pkill` alone does not settle this. Either:

    launchctl setenv OLLAMA_MAX_LOADED_MODELS 1   # then restart Ollama.app

or quit Ollama.app and let `dev-local.sh` start the daemon itself. To confirm what
the running daemon actually has:

    ps eww -o command= -p "$(pgrep -f 'ollama serve' | head -1)" | tr ' ' '\n' | grep OLLAMA

**Ollama not running at all** — `dev-local.sh` starts it. Started by hand, the
control plane's first `/api/version` call fails and activation lands in `error`
with `ollama at http://127.0.0.1:11434/… did not answer`.

## An activation is stuck in `loading`

`/admin/state` gives `progress_hint` and `since`; `/admin/logs?source=control` gives
the phase and how long each step took:

    activation requested: model=… backend=ollama model_ref=… job=… from=…
    drain complete after 0 ms (0 request(s) were in flight)
    unload complete after 157 ms
    preload accepted by the ollama backend after 4896 ms
    model … is ready on the ollama backend: load 4898 ms, switch 4909 ms total

Read it as: where did it stop?

| Last line | Meaning |
|---|---|
| `activation requested` only | stuck draining — a `/v1` stream is still open. It gives up on its own; `docs/SPEC.md` §5 has the budget |
| `drain complete` | stuck unloading — see the next section |
| `preload accepted` | the weights are loading. First load of a tag also *pulls* it, which is minutes, not seconds. `ollama pull <tag>` by hand to watch progress |

Nothing hangs forever: the load times out and the state becomes `error` with the
reason in `last_error`. If `preload accepted` never appears and the log shows a
404, the `model_ref` in `models.yaml` is not a tag the daemon has — the log names
it, and `ollama list` shows what is actually there.

## A model will not unload

    'glm-4.7-flash:q4_K_M' was still loaded 30s after being unloaded
    (120 polls of /api/ps, last saw [...]); refusing to activate another model on top of it

The activation fails instead of loading on top. That refusal is the point: both
models fit in 48 GB, so proceeding would succeed silently and leave the daemon
holding two while `/admin/state` reported one — the wrong model would answer and
nothing would say so. Check the daemon:

    curl -s http://127.0.0.1:11434/api/ps

If it holds a model nobody asked for, restart the daemon. If it holds two,
`OLLAMA_MAX_LOADED_MODELS` was wrong when it started (above), and `smoke.sh` L5 is
the check that would have caught it.

## `state: error` after the daemon crashed

`/admin/state` goes to `error` within about 5 s and stays there. **There is no
automatic restart — ADR-0005.** A crash loop hides the failure that caused it, and
a model that silently comes back is worse than one that plainly did not.

Recovery is one call, once the daemon is back:

    curl -X POST -H "Authorization: Bearer $KEY" \
      http://127.0.0.1:8080/admin/models/glm-4.7-flash/activate

Or pick the model again in the desktop app's dropdown, which does the same thing.
The control plane itself stays up throughout — `/healthz` keeps returning 200, which
is how the app can still show you the error.

## Port conflicts

| Port | Who | If taken |
|---|---|---|
| 8080 | control plane | `dev-local.sh` refuses to start: *something is already serving 127.0.0.1:8080*. `lsof -nP -iTCP:8080 -sTCP:LISTEN`. Usually a control plane you forgot; `HARNESS_PORT=8081 ./scripts/dev-local.sh` if you want both |
| 11434 | ollama | almost always another Ollama (the app, or a stray `ollama serve`). Do not run a second one — they share the model store. `OLLAMA_PORT=11435 ./scripts/dev-local.sh` only if you know why |

## The API key

Generated once by `dev-local.sh` into `.env.local` (gitignored, mode 600) and
printed in its banner. It lives nowhere else in the repo, and the redaction filter
keeps it out of every log record and out of `/admin/logs`.

To change it: stop the control plane, edit or delete the `HARNESS_API_KEY` line in
`.env.local` (deleting makes `dev-local.sh` generate a fresh one), restart, and
paste the new key into the desktop app's Settings. Every existing client 401s until
it does — there is no grace period, by design.

`scripts/rotate-key.sh` is a stub for the Azure path — it does nothing yet, and
nothing on this path needs it.

---

## The Azure path

**There is no runbook, because there is no deployment.** `scripts/provision.sh`,
`vm-start.sh`, `vm-stop.sh` and `rotate-key.sh` are two-line stubs; the systemd
unit, the logrotate config and the env example in `deploy/` are empty files; spot
eviction handling, auto-shutdown, the NSG rules and reboot recovery are unwritten.
Nothing above this line applies to that path.

The `vllm` backend itself is a different matter — `supervisor/backends/vllm.py` is
implemented and covered by the shared contract suite (`docs/BACKENDS.md` §4.1).
The gap is the tooling to stand a VM up, and the fact that none of it has met a
GPU.

This is deliberate scope, not unfinished work — `docs/BACKLOG.md` lists it under
**M4 (Azure, deferred)**. When quota arrives, `docs/BACKENDS.md` §4.1 is the
migration, and that section is the thing to trust; this file will need rewriting
against a real VM at that point.
