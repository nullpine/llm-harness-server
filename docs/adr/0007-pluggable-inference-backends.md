# ADR-0007: Pluggable inference backends

**Status:** accepted — amends ADR-0001
**Date:** 2026-08-26

## Context

ADR-0001 chose vLLM as the inference engine. That decision assumed an Azure GPU
VM, and it is still the right choice *for that hardware*. It is not available on
the hardware we actually have today.

Azure GPU families default to zero quota on a new subscription, and free-tier
subscriptions are not eligible for the increase — Microsoft's guidance is to move
to pay-as-you-go for N-series. Spot quota is tracked separately from standard
quota, so an H100 needs two approvals, and H100 is the hardest tier to get. The
project is on free tier and staying there for now.

Meanwhile a 32 GB Apple Silicon MacBook runs both spec'd models comfortably at
4-bit: GLM-4.7-Flash is a 30B MoE with 3B active, and Qwen3.8-27B fits with room
to spare. Ollama serves an OpenAI-compatible API at `http://localhost:11434/v1`,
including streaming, and manages weights itself.

So the engine has to become a choice, not a constant. The alternative — building
against mocks until GPU quota appears — defers every integration problem to the
end of the project, which is where they are most expensive.

## Decision

Introduce a **`Backend` interface** in `supervisor/`. The supervisor owns the state
machine, the activation lock, and the job registry, exactly as before; the backend
owns only *how a model is made to serve*.

    class Backend(Protocol):
        name: str
        base_url: str                                    # where /v1 lives
        async def activate(self, spec: ModelSpec) -> None
        async def stop(self) -> None
        async def health(self) -> bool
        async def progress_hint(self) -> str | None
        async def resources(self) -> list[ResourceInfo]  # may be empty

Three implementations:

| Backend | Activation | Stop | Use |
|---|---|---|---|
| `ollama` | preload via `POST /api/generate` with the model and no prompt | `keep_alive: 0` | local Mac, today |
| `vllm` | spawn a process group, poll `/health` | SIGTERM the group, wait for VRAM release | Azure GPU VM, later |
| `remote_openai` | no-op (validate the model is listed) | no-op | hosted provider, or our own remote deployment |

`models.yaml` gains a `backend:` key per entry. `HARNESS_DEFAULT_BACKEND` sets the
fallback.

**The externally visible API does not change.** `docs/API-CONTRACT.md` is untouched
— no version bump, no hash break. The desktop app cannot tell which backend is
serving, and must not be able to.

**The single-active-model rule stays**, even for `remote_openai` where nothing
forces it. It is a product decision — one dropdown, one model — not a resource
constraint, and keeping it uniform means the state machine has one shape.

## Consequences

**Good**

- The project runs today, free, on hardware already owned, with the two models
  from the spec rather than substitutes.
- Integration is exercised from M1 instead of deferred. The streaming proxy, the
  supervisor state machine, the abort path, and the dropdown all get tested against
  a real engine.
- Moving to an Azure GPU later is a deployment change plus a one-line catalog edit.
  See `docs/BACKENDS.md` §4.
- `remote_openai` keeps the hosted-provider path open at a cost of roughly thirty
  lines, which also makes "everyday hosted, private when it matters" possible later.

**Bad**

- One more abstraction in the supervisor, and two more code paths to test. Mitigated
  by the fact that `fake_vllm` already forced a seam here.
- Backends have genuinely different characteristics that leak into the catalog:
  Ollama loads an 18 GB model from local SSD in 10–30 s, vLLM cold-starts in 60–120 s.
  `estimated_load_seconds` is therefore per-model *and* per-backend.
- `/admin/state.gpu` is meaningless for Ollama on a Mac and for remote backends.
  It returns an empty array — already valid per the contract, but it means the
  desktop app must render a missing GPU section gracefully rather than assume one.
- Ollama does not support `logprobs`, `logit_bias`, `n`, or `tool_choice`. None are
  used by the MVP, but tool calling is a post-MVP item that will need vLLM or a
  hosted backend.

## Alternatives considered

| Option | Why not |
|---|---|
| Wait for Azure GPU quota | Requires pay-as-you-go, which is declined; and H100 quota is not guaranteed even then |
| Build against `fake_vllm` and the mock until quota appears | Defers all integration risk to the end of the project |
| Drop vLLM entirely and standardise on Ollama | Gives up throughput, FP8, and tool-call parsing on real GPU hardware; the Azure path is still the production target |
| Run the model locally and skip the control plane — the app talks to Ollama directly | Ollama has no auth, no single-active-model enforcement, and no `/admin` surface. It also abandons the harness, which is the point of the project |
| Smaller models so a free-tier CPU VM could serve them | A CPU-served 8B model is slow enough to make the UI feel broken, and the quality drop is severe |
