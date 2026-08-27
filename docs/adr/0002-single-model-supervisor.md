# ADR-0002: One model at a time, switched by kill-and-respawn

**Status:** accepted
**Date:** 2026-08-25

## Context

The machine holds one model's weights at serving quality. vLLM serves exactly one
model per process and offers no "load this instead" call, so switching means
process lifecycle: stop what is running, wait for the memory to actually come
back, start the next one.

Two alternatives were considered.

**Run both at once.** Ruled out on an H100 by VRAM, and rejected on principle: the
whole point of the harness is that the answer to "which model am I talking to?"
is a fact, not a guess.

**vLLM's `--enable-sleep-mode`.** Level-2 sleep keeps a process alive with its
weights offloaded, which would make switching seconds rather than a minute. But it
is built for RLHF weight updates, not for serving two unrelated models, and it
adds a failure mode — a process that is alive, holding memory, and not serving —
that the state machine would have to model and the operator would have to
diagnose. Kill-and-respawn is boring and correct.

## Decision

A single **supervisor** owns the engine process and a state machine:

    idle → loading → ready → stopping → (loading | idle)
                  ↘ error ↙

- Every spawn and kill goes through one module. No `subprocess.Popen` anywhere
  else in the package.
- Activation is asynchronous: `POST /admin/models/{id}/activate` returns 202 with
  a job id immediately, and `/admin/state` reports progress. A concurrent second
  activation gets 409 `activation_in_progress`.
- A switch drains in-flight completions, stops the current model, **verifies the
  memory was actually released**, and only then starts the next.
- No sleep mode in the MVP.

## Consequences

- Switching costs a full load — tens of seconds. The catalog advertises
  `estimated_load_seconds` per model so the client can show an honest countdown
  instead of a spinner, and `/v1` returns 503 `model_loading` with `Retry-After`
  rather than hanging.
- The unload verification is not optional and not merely defensive. On the local
  path both models fit in 48 GB, so an unload that silently failed would leave two
  models resident while `/admin/state` reported one — the wrong model would answer
  and nothing would notice. The backend polls until the old model is gone and
  fails the activation rather than loading on top of it (smoke L5).
- One activation at a time means the lock is held across a slow operation, so it
  must never be held by a request handler.
- Revisiting sleep mode is a post-MVP item, tracked in `docs/BACKLOG.md`.
