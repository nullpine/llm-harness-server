# ADR-0001: vLLM as the inference engine, not Ollama

**Status:** accepted — **amended by [ADR-0007](0007-pluggable-inference-backends.md)**
**Date:** 2026-08-25 (amended 2026-08-26)

> **Read ADR-0007 first.** The reasoning below still holds *for a GPU deployment*,
> but the conclusion — "vLLM, singular" — does not. ADR-0007 makes the engine
> pluggable and makes Ollama the default for local hardware, which is what the MVP
> actually ships on. Nothing in this ADR was found to be wrong; the premise it
> assumed (that an Azure H100 would exist) did not arrive.

## Context

The harness needs an OpenAI-compatible server in front of open-weights models. The
two candidates were vLLM and Ollama, and at the time the target was a single Azure
`NC40ads_H100_v5`.

They optimise for different things. vLLM is a serving engine: continuous batching,
paged attention, FP8 weights, and throughput that scales with concurrent requests.
Ollama is a local runner: GGUF quantisation, a model store, automatic load and
unload, and an emphasis on being easy to start.

On an H100 the difference is not subtle. FP8 at full throughput is the reason to
rent the hardware at all; serving 4-bit GGUF from an H100 pays H100 prices for a
fraction of the machine.

## Decision

Use **vLLM**, and treat the engine as an implementation detail behind our own HTTP
surface so the choice can be revisited without breaking clients.

Specifically: vLLM runs as a child process of the control plane on
`127.0.0.1:8000`, model-specific flags live in `models.yaml` rather than in Python,
and the version is pinned in `requirements.lock` because vLLM moves CLI flags
between releases.

## Consequences

- The quality ceiling is the hardware's, not the quantiser's: FP8 weights, the
  real context window, and the tool-call and reasoning parsers each model needs.
- Model switching becomes our problem. vLLM serves one model per process and has
  no "load this instead" API, which is what [ADR-0002](0002-single-model-supervisor.md)
  exists to solve. Ollama would have handled that for us.
- Nothing runs on a laptop. vLLM needs CUDA, so without a GPU there is no way to
  exercise the system end to end — a cost paid daily until ADR-0007.
- The pinned version is load-bearing. An unpinned upgrade breaks `models.yaml`
  args with no warning.

## Amendment (ADR-0007)

GPU quota did not arrive, and the engine choice turned out to be separable from
everything above it — the state machine, the proxy, the auth, and the contract are
all indifferent to what serves the tokens. ADR-0007 introduces a `Backend`
Protocol with three implementations (`ollama`, `vllm`, `remote_openai`) and makes
`ollama` the MVP default. `vllm.py` is written to this ADR's spec and stays
maintained; it is simply not the path that ships first.
