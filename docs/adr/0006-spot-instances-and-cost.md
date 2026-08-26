# ADR-0006: Run the GPU VM as Spot, and be honest about what self-hosting costs

**Status:** accepted
**Date:** 2026-08-25

## Context

A `Standard_NC40ads_H100_v5` is **$6.98/hour on-demand** — about $5,095/month if
left running, or $628/month at three hours a day. That is the single largest fact
about this project, and it is not caused by the model choice.

GLM-4.7-Flash (30B-A3B MoE) and Qwen 3.8 27B are already near the small end of what
is worth self-hosting. The cost comes from the hardware floor: any Azure VM with a
GPU capable of serving a 27–30B model at usable speed starts around $3/hour
on-demand. Shrinking the model to 8B would let us drop to a half-A10 at $1.60/hour
and would meaningfully degrade output quality — a bad trade.

The deeper issue is **utilization**. One person chatting keeps an H100 busy roughly
1–3 % of the time it is rented. The GPU can serve thousands of tokens per second
across dozens of concurrent requests; a single user typing consumes almost none of
that. We are paying for a whole machine to serve one keyboard.

For comparison, the same open-weights models are served by hosted providers at
about **$0.06/M input and $0.40/M output**. A heavy personal workload — call it 12M
input and 2M output tokens a month — costs roughly **$1.50–2.00**. Even at ten
times that volume it stays under $20.

## Decision

1. **Provision the VM as Spot**, with `--priority Spot --eviction-policy Deallocate
   --max-price -1`. Spot is **$1.29/hour**, an 81 % reduction, and eviction merely
   deallocates the VM — `/mnt/models` and `/etc/harness` survive.
2. **Do not downgrade the GPU to save money.** Spot H100 at $1.29/hour is both
   cheaper and far more capable than an on-demand A10 at $3.20/hour. Choosing a
   smaller GPU is the wrong lever.
3. **Treat teardown as a product feature**, not documentation. `vm-stop.sh`,
   `vm-start.sh`, and a DevTest auto-shutdown schedule ship in the MVP, and the
   README leads with the hourly rate.
4. **State the economics in the spec.** Anyone picking this project up should know
   within thirty seconds that self-hosting is roughly two orders of magnitude more
   expensive than an API for a single user.

## Consequences

**Good**

- ~$116/month at three hours a day instead of ~$628 — the difference between a
  hobby project and an abandoned one.
- The desktop app already models `unreachable` and recovers automatically, so
  eviction needs no new client behaviour.
- The decision is documented, so nobody re-litigates it in month three.

**Bad**

- Spot capacity is not guaranteed. `vm-start.sh` can fail with a capacity error and
  the user has to retry or pick another region. `scripts/vm-start.sh` must surface
  that clearly rather than failing silently.
- Eviction mid-conversation drops an in-flight response. Acceptable for a
  single-user tool; unacceptable if this ever serves other people, at which point
  this ADR should be revisited alongside on-demand or reserved capacity.
- The honest cost framing may lead a reader to conclude they should just use an
  API. That is a legitimate conclusion and the project is better for surfacing it
  early rather than after $3,000 of spend.

## Alternatives considered

| Option | Why not |
|---|---|
| Smaller GPU (A10 24 GB, ~14B model at FP8) | On-demand A10 costs more than spot H100 and serves a weaker model |
| Smaller model on the same GPU | Does not change the hardware floor; the GPU is the cost, not the weights |
| Reserved instances | Not offered for this SKU, and a 1–3 year commitment is wrong for an MVP |
| Scale-to-zero: app starts the VM on first message | Genuinely good, but a 3–5 minute cold start plus Azure credentials in the desktop app is post-MVP scope (roadmap item 12) |
| Use a hosted API instead | ~100× cheaper for one user, but gives up tenant isolation, version pinning, and the harness itself — which is the point of the project |
