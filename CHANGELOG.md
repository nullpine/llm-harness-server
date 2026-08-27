# Changelog

## [0.1.0] — 2026-08-26

First release. A control plane for self-hosted open-weights models.

- OpenAI-compatible chat completions with true streaming, over a pluggable
  backend (Ollama locally, vLLM for GPU deployments, or a remote provider)
- One model active at a time, switched via `/admin/models/{id}/activate`, with
  drain, unload verification, and a watchdog
- Bearer auth on every route except `/healthz`
- Control-plane lifecycle logging via `/admin/logs`, so a failed activation is
  diagnosable without shell access

Serves API contract v1.1 (`docs/API-CONTRACT.md`). The contract version and this
one move independently: `/healthz` reports both.

Deliberately not included: the Azure GPU deployment path (see `docs/BACKENDS.md`
§4.1 and `docs/BACKLOG.md`), tool calling, vision, embeddings.
