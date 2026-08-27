# ADR-0003: The engine is a child process, not a systemd template unit

**Status:** accepted
**Date:** 2026-08-25

## Context

On the deployed path the control plane runs under systemd. The engine process has
to be started, stopped, and — above all — *reliably* killed, because a survivor
holds 30+ GB of VRAM and the next activation OOMs.

The obvious systemd-native design is a template unit, `harness-model@glm.service`,
with the control plane calling `systemctl start`/`stop`. systemd would then own
process supervision, restart policy, logging, and cgroup teardown.

## Decision

Run the engine as a **child process of the control plane**, and give the control
plane's own unit `KillMode=control-group`.

- spawned with `start_new_session=True`, so we own the process group
- killed by **process group** — SIGTERM, then SIGKILL after 15 s
- `wait_for_vram_release()` polls before every spawn and fails the activation with
  a clear error rather than launching into an OOM
- any orphan holding the engine port is killed on control-plane startup

## Consequences

- One lifecycle owner, not two. With a template unit, "what is running?" has two
  answers — systemd's and the supervisor's — and they disagree exactly when
  something has gone wrong.
- Local development matches production. `dev-local.sh` starts the same code path
  with no systemd at all, which is what makes the whole suite runnable without a
  GPU or a VM.
- We inherit the work systemd would have done: process-group kills, the escalation
  timeout, and orphan cleanup are ours to get right, and `KillMode=control-group`
  is what stops a control-plane restart from leaking the child.
- A template unit per model is a genuine hardening upgrade — cgroup limits and
  independent restart policy per model — and is **deliberately deferred**, not
  rejected. See `docs/SPEC.md` §5.6.
