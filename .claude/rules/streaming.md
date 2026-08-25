---
description: Anti-buffering rules for the /v1 streaming proxy
paths:
  - "src/harness_control/proxy.py"
  - "src/harness_control/routes/openai.py"
  - "src/harness_control/app.py"
  - "deploy/caddy/**"
  - "tests/test_proxy_streaming.py"
  - "tests/test_proxy_abort.py"
---

# Streaming proxy — do not let anything buffer

If any layer buffers, streaming silently degrades to one blob at the end and every
test that only checks the final text still passes. All four defences are required:

1. **`httpx` iterated with `aiter_raw()`** — never `aiter_text`, never `.json()`,
   never `aiter_lines()` on the relay path.
2. **No compression or JSON middleware on `/v1/*`.** No `GZipMiddleware`, no
   response-model validation on the streaming route.
3. **Caddy `flush_interval -1`** on `/v1/*`, and `encode` must not apply there.
4. **`tests/test_proxy_streaming.py` asserts inter-chunk arrival times**, not just
   the concatenated content. A test that only checks the final string does not
   catch this class of bug.

Required response headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`,
`Content-Type: text/event-stream`.

## Abort propagation

Client disconnect must cancel the upstream vLLM request. Close the `httpx`
response in a `finally` block. An orphaned generation burns GPU time on a $7/hour
machine with nobody reading the output.

## State guards

Before proxying, check supervisor state and return the contract's response:
`idle`/`error` → 409 `model_not_active`; `loading`/`stopping` → 503 `model_loading`
with `Retry-After`; requested model ≠ active model → 409 with
`details: {active, requested}`. Never hang, never 500.

Full detail: `docs/API-CONTRACT.md` §2, `docs/SPEC.md` §5.3.
