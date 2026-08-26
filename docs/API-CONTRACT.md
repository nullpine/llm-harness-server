# LLM Harness — API Contract v1.1

**Status:** normative for MVP. Both `llm-harness-desktop` and `llm-harness-server` MUST conform.
**Owner of truth:** this file is committed to **both** repos at `docs/API-CONTRACT.md`. Any change is a PR in both, and bumps the version at the top.

---

## 1. Transport & base

| | |
|---|---|
| Scheme | HTTPS only (TLS terminated by Caddy) |
| Base URL | `https://<host>` — e.g. `https://harness.example.com` |
| Content type | `application/json`, except streaming (`text/event-stream`) |
| Auth | `Authorization: Bearer <API_KEY>` on every route except `GET /healthz` |
| Client identity | `X-Harness-Client: llm-harness-desktop/<semver>` (informational) |

### Auth failures

| Condition | Status | Body |
|---|---|---|
| Missing header | 401 | `{"error":{"code":"unauthorized","message":"..."}}` |
| Bad key | 401 | same |
| Key valid, route admin-only and key is read-only (post-MVP) | 403 | `{"error":{"code":"forbidden",...}}` |

The server MUST compare keys in constant time (`hmac.compare_digest`).

### Error envelope

Every non-2xx response uses:

```json
{ "error": { "code": "model_not_active", "message": "human readable", "details": {} } }
```

Codes used in MVP: `unauthorized`, `forbidden`, `not_found`, `model_not_active`,
`model_loading`, `activation_in_progress`, `activation_failed`, `upstream_unavailable`,
`bad_request`, `internal`.

---

## 2. Inference routes (OpenAI-compatible)

These are a thin proxy in front of the running vLLM process. The wire format is
OpenAI Chat Completions so the desktop app can use any OpenAI-compatible SDK.

### `GET /v1/models`

Returns **only the currently active model** (OpenAI semantics: what you can call right now).
Use `GET /admin/models` for the full catalog.

```json
{
  "object": "list",
  "data": [
    { "id": "glm-4.7-flash", "object": "model", "owned_by": "harness", "created": 1756000000 }
  ]
}
```

If no model is active: `{"object":"list","data":[]}` with status 200.

### `POST /v1/chat/completions`

Request — subset of the OpenAI schema the MVP supports:

```json
{
  "model": "glm-4.7-flash",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "Hello" }
  ],
  "stream": true,
  "temperature": 0.7,
  "top_p": 0.95,
  "max_tokens": 2048,
  "stop": ["</s>"]
}
```

Rules:

- `model` MUST equal the active model id. If it does not, respond **409** with
  `model_not_active` and `details: {"active": "<id or null>", "requested": "<id>"}`.
  The desktop app treats this as "your dropdown is stale" and refreshes state.
- If a model is mid-load, respond **503** with `model_loading` and
  `Retry-After: <seconds estimate>`.
- If vLLM is unreachable, respond **502** with `upstream_unavailable`.
- Unknown fields are forwarded to vLLM unchanged (forward-compatible).

Non-streaming response is the OpenAI object verbatim from vLLM.

Streaming response (`stream: true`) is SSE, verbatim vLLM frames:

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{"content":"He"},"index":0}]}

data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"llo"},"index":0}]}

data: {"id":"chatcmpl-...","choices":[{"delta":{},"finish_reason":"stop","index":0}],"usage":{...}}

data: [DONE]

```

- Server MUST set `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`.
- Server MUST NOT buffer; it streams chunks through as they arrive.
- Client disconnect MUST cancel the upstream vLLM request (abort propagation).
- Reasoning models may emit `delta.reasoning_content` (vLLM) or `delta.reasoning`
  (Ollama). Clients MUST tolerate both; MVP desktop renders either in a collapsed
  Thinking block.
- Frames are relayed verbatim, so `model` in a chunk carries the engine's own name
  for the model, not the catalog id the client requested. Clients MUST correlate
  responses by their own request, never by the frame's `model` field.

---

## 3. Control routes

### `GET /healthz` — unauthenticated

```json
{ "status": "ok", "version": "1.1", "service_version": "0.1.0", "uptime_s": 1234 }
```

`version` is the API contract version the server implements — clients compare this
for compatibility. `service_version` is the server build, for debugging only;
clients MUST NOT branch on it.

Used by Caddy/Azure health probes. Never reveals model or key info.

### `GET /admin/models`

The full catalog from `models.yaml`, annotated with live state.

```json
{
  "active_model_id": "glm-4.7-flash",
  "state": "ready",
  "models": [
    {
      "id": "glm-4.7-flash",
      "display_name": "GLM 4.7 Flash",
      "model_ref": "glm-4.7-flash:q4_K_M",
      "params": "30B-A3B (MoE)",
      "quantization": "q4_K_M",
      "context_length": 32768,
      "available": true,
      "state": "ready",
      "estimated_load_seconds": 25
    },
    {
      "id": "qwen3.8-27b",
      "display_name": "Qwen 3.8 27B",
      "model_ref": "Qwen/Qwen3.8-27B-FP8",
      "params": "27B (dense, VL)",
      "quantization": "fp8",
      "context_length": 262144,
      "available": true,
      "state": "idle",
      "estimated_load_seconds": 110
    }
  ]
}
```

`model_ref` is backend-specific — an Ollama tag, a Hugging Face repo, or a provider
model string. Clients MUST treat it as opaque.

`available` means the backend can serve this model without a fetch.

### `GET /admin/state`

Cheap, pollable (client polls at 2s while an activation is in flight, 30s otherwise).

```json
{
  "state": "loading",
  "active_model_id": "qwen3.8-27b",
  "previous_model_id": "glm-4.7-flash",
  "since": "2026-08-25T18:04:11Z",
  "progress_hint": "downloading weights (3.1/16.0 GB)",
  "last_error": null,
  "gpu": [
    { "index": 0, "name": "NVIDIA H100 NVL", "memory_used_mb": 41210, "memory_total_mb": 95830, "utilization_pct": 0 }
  ]
}
```

**State machine** — `state` is one of:

```
idle ──activate──> loading ──ready──> ready
  ▲                   │                 │
  │                   └──fail──> error  │
  │                                │    │
  └────────── stopping <───────────┴────┘
```

| State | Meaning | `/v1/chat/completions` behaviour |
|---|---|---|
| `idle` | No model loaded | 409 `model_not_active` |
| `loading` | vLLM starting / weights loading | 503 `model_loading` |
| `ready` | Serving | normal |
| `stopping` | Draining before a switch | 503 `model_loading` |
| `error` | Last activation failed; see `last_error` | 409 `model_not_active` |

### `POST /admin/models/{model_id}/activate`

Body: `{ "force": false }` (optional; `force: true` skips the in-flight drain).

| Situation | Status | Body |
|---|---|---|
| Accepted | 202 | `{"job_id":"act_01J...","model_id":"qwen3.8-27b","estimated_seconds":110}` |
| Already active and `ready` | 200 | `{"job_id":null,"model_id":"...","already_active":true}` |
| Another activation in flight | 409 | `activation_in_progress` |
| Unknown model id | 404 | `not_found` |

Activation is **asynchronous**. The client polls `GET /admin/state` (or `GET /admin/jobs/{job_id}`)
until `state` is `ready` or `error`.

Server behaviour on activate:

1. Set `state = stopping`. Stop accepting new `/v1` requests (503 `model_loading`).
2. Wait up to `DRAIN_TIMEOUT_S` (default 30) for in-flight completions to finish, then SIGTERM the vLLM process group; SIGKILL after 15s.
3. Set `state = loading`, spawn vLLM for the new model.
4. Poll vLLM `/health` until 200 or `LOAD_TIMEOUT_S` (default 900) → `ready` or `error`.

### `GET /admin/jobs/{job_id}`

```json
{ "job_id":"act_01J...", "model_id":"qwen3.8-27b", "status":"running",
  "started_at":"...", "finished_at":null, "error":null, "log_tail":["..."] }
```

`status` ∈ `queued | running | succeeded | failed | cancelled`.

### `GET /admin/logs?lines=200&source=vllm`

```json
{ "source": "vllm", "lines": ["INFO 08-25 18:04:11 ...", "..."] }
```

`source` ∈ `vllm | control`. Capped at 1000 lines. Used by the desktop app's
"Server logs" panel when a load fails.

---

## 4. Timeouts & retries (client obligations)

| Operation | Client timeout | Retry policy |
|---|---|---|
| `GET /healthz`, `/admin/*` | 10 s | 2 retries, exponential backoff 500ms → 2s |
| `POST .../activate` | 15 s (the 202, not the load) | no auto-retry |
| Polling `/admin/state` during load | 10 s | poll every 2 s, up to 15 min |
| `POST /v1/chat/completions` | no total timeout; **60 s idle timeout between SSE chunks** | never auto-retry a stream that emitted tokens |

---

## 5. Versioning

- Path is unversioned beyond `/v1` (OpenAI shape) and `/admin`.
- The server reports its contract version in `GET /healthz` → `version`.
- The desktop app warns (non-blocking) if the server minor version is ahead of its own.
