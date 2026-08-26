---
description: What belongs in a Backend and what belongs in the supervisor
paths:
  - "src/harness_control/supervisor/**"
  - "src/harness_control/catalog.py"
  - "src/harness_control/proxy.py"
---

# Backend boundary

The supervisor owns the state machine, the activation lock, the drain, the job
registry, and the watchdog. A backend owns only *how a model is made to serve*.

- A backend never sets state. It reports facts; the supervisor decides state.
- No `if backend.name == "ollama"` branching outside `backends/`. If a caller
  needs to know which backend it has, the interface is missing a method.
- `proxy.py` reads `backend.base_url` and nothing else about the backend.
- New backends must satisfy the same contract tests. `tests/test_backends.py`
  runs the shared suite against every registered implementation.
- `docs/API-CONTRACT.md` must never change to accommodate a backend. If a backend
  cannot meet the contract, that is a reason to reject the backend.

Full detail: `docs/BACKENDS.md`, ADR-0007.
