# ADR-0005: A failed model stays failed until a client asks again

**Status:** accepted
**Date:** 2026-08-25

## Context

Models fail to load, and engines die. Bad weights, a `model_ref` that is not a tag
the daemon has, an OOM, a daemon killed out from under us. The supervisor notices —
either the load times out or the watchdog sees the backend stop answering — and the
state machine goes to `error`.

The reflex is to retry. systemd does it by default (`Restart=always`), and it is
the right behaviour for a stateless web process.

It is the wrong behaviour here. A model load is expensive, and the failures above
are mostly deterministic: retrying a bad `model_ref` produces the same 404 forever,
while retrying an OOM produces an OOM every time and burns GPU minutes doing it. A
crash loop also *destroys the evidence* — the log fills with identical failed
attempts, and the state flickers between `loading` and `error` so the client can
never render a stable, explainable failure.

## Decision

**No automatic restart of a failed model.** The state stays `error`, with
`last_error` set, until a client activates something.

- the control plane itself stays up and keeps serving `/healthz` and `/admin/*` —
  that is how the app can show you the error at all
- the watchdog reports a dead backend; it does not resurrect it
- the failure is logged once, with the phase it died in, the backend's own words,
  and an explicit note that nothing will retry
- recovery is exactly one call: activate a model

`Restart=always` on the *control plane's* unit is unaffected. The control plane is
stateless and should come back; the model it was serving should not come back by
itself.

## Consequences

- Recovery is manual — one click in the desktop app's dropdown, or one `POST` to
  `/admin/models/{id}/activate`. There is no unattended self-healing, by design.
- An overnight failure stays failed until someone looks. On a single-user system
  that is the honest outcome; nobody was being served in the meantime anyway.
- The failure is diagnosable afterwards, because it happened once and the log says
  what it was. `docs/OPERATIONS.md` reads directly off it.
- This is asserted by tests (`test_a_failure_does_not_crash_loop`,
  `test_l8_the_watchdog_does_not_restart_anything`) so that a future "helpful"
  retry cannot be added without arguing with this ADR first.
