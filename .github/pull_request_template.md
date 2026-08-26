## What

<!-- One paragraph. Which backlog item (docs/BACKLOG.md) does this close? -->

Backlog: M_ — <item>

## Why

<!-- Link the spec section or acceptance criterion this satisfies, e.g. SPEC §5.2, B9. -->

## Checks

- [ ] `make lint` clean (ruff check, ruff format --check, mypy --strict)
- [ ] `make test` green — no test imports torch or needs a GPU
- [ ] `shellcheck` clean if `scripts/` changed
- [ ] No secrets: no API key, hostname, or subscription id in the diff
- [ ] `docs/API-CONTRACT.md` untouched, **or** the matching PR in
      `llm-harness-desktop` is linked below and the contract version is bumped
- [ ] Every spawn/kill still goes through `supervisor/process.py`
- [ ] If `/v1/*` changed: `aiter_raw`, no compression middleware, and
      `tests/test_proxy_streaming.py` still asserts incremental arrival

## Spec deviations

<!-- If the spec was ambiguous or wrong, say so here and propose the fix.
     Silently picking an interpretation is the thing we are avoiding. -->

None.
