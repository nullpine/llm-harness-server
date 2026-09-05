"""The `/v1/*` streaming relay.

Read `.claude/rules/streaming.md` before changing anything here. The failure mode
this file is built against is silent: if any layer buffers, the client still gets
the right text, just all at once at the end, and every test that checks only the
final string still passes.

The four defences, three of which live here:

1. `aiter_raw()` — no decoding, no line-splitting, no `.json()` on the relay path.
2. No compression or response-model middleware on `/v1/*` (see `app.py`; the route
   returns a `StreamingResponse` and declares no `response_model`).
3. Caddy `flush_interval -1` — `deploy/caddy/Caddyfile.template`.
4. `tests/test_proxy_streaming.py` asserts inter-chunk arrival times.

And abort propagation: `resp.aclose()` in a `finally`, so a client hanging up
cancels the upstream generation instead of burning GPU time for nobody.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from harness_control.errors import AppError, ErrorCode
from harness_control.supervisor.state import LOADING_STATES, ModelState
from harness_control.supervisor.supervisor import Supervisor

log = logging.getLogger(__name__)

#: Streaming responses have no total deadline — a long answer is not a hung one.
#: The read timeout bounds the gap *between* chunks, which is the real failure.
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

#: Required by the contract §2 and by `.claude/rules/streaming.md`.
STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

#: Hop-by-hop headers must not be forwarded verbatim; the rest of the upstream's
#: headers are ours to re-derive, not to copy.
_DROP_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
)


def guard_state(supervisor: Supervisor, requested_model: str | None) -> None:
    """Raise the contract's answer for anything that is not `ready` and matching.

    Never hangs, never 500s — contract §3's state table, and acceptance L6 and L7.
    """
    state = supervisor.state
    if state in LOADING_STATES:
        # The incoming model, not the active one: mid-switch there is no active
        # model, and answering "10" when the catalog says 35 sends the client
        # back three times too early.
        loading = supervisor.pending_model_id or supervisor.active_model_id or ""
        retry_after = supervisor.estimated_seconds(loading) or 10
        raise AppError(
            ErrorCode.MODEL_LOADING,
            "a model is loading; retry shortly",
            details={"state": state.value, "active": supervisor.active_model_id},
            headers={"Retry-After": str(retry_after)},
        )

    active = supervisor.active_model_id
    if state is not ModelState.READY or active is None:
        raise AppError(
            ErrorCode.MODEL_NOT_ACTIVE,
            "no model is currently active",
            details={"active": active, "requested": requested_model, "state": state.value},
        )

    if requested_model is not None and requested_model != active:
        # The desktop app reads this as "your dropdown is stale" and refreshes.
        raise AppError(
            ErrorCode.MODEL_NOT_ACTIVE,
            f"{requested_model!r} is not the active model",
            details={"active": active, "requested": requested_model},
        )


async def relay_chat_completions(
    request: Request,
    supervisor: Supervisor,
    client: httpx.AsyncClient,
    body: dict[str, Any],
) -> Response:
    """Proxy one chat completion upstream, streaming or not."""
    base_url = supervisor.base_url()
    if base_url is None:  # pragma: no cover - guard_state has already rejected this
        raise AppError(ErrorCode.MODEL_NOT_ACTIVE, "no backend is active")

    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    # The client addresses the model by its catalog id; the engine knows it by
    # `model_ref`. Substituting here — rather than branching on the backend — keeps
    # `.claude/rules/backend-boundary.md` intact: `model_ref` *is* the upstream name.
    outgoing = {**body, "model": supervisor.active_model_ref or body.get("model")}
    upstream_request = client.build_request(
        "POST",
        url,
        json=outgoing,
        headers=_forwarded_request_headers(request, supervisor),
        timeout=STREAM_TIMEOUT,
    )

    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        log.warning("upstream %s unreachable: %s", url, exc)
        raise AppError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            f"the model backend at {base_url} is unreachable",
            details={"reason": str(exc)},
        ) from exc

    if not body.get("stream"):
        return await _buffered_response(upstream)

    if upstream.status_code >= 400:
        # An upstream error is a normal body, not a stream. Read it, close it, and
        # report it as the envelope rather than relaying an error as SSE frames.
        return await _upstream_error_response(upstream)

    return StreamingResponse(
        _relay(upstream, url, supervisor),
        status_code=upstream.status_code,
        media_type="text/event-stream",
        headers=dict(STREAM_HEADERS),
    )


async def _relay(
    upstream: httpx.Response, url: str, supervisor: Supervisor
) -> AsyncIterator[bytes]:
    """The relay itself. `aiter_raw` in, bytes out, `aclose` guaranteed.

    Wrapped in the supervisor's in-flight counter so a model switch can drain
    real work instead of guessing. The counter is released in `__aexit__`, which
    runs on an abort as well as on completion — a client hanging up mid-switch
    must not stall the drain for its full timeout.
    """
    completed = False
    async with supervisor.track_request():
        try:
            # aiter_raw: no decoding and no line buffering. aiter_text or
            # aiter_lines here is the bug this whole module exists to prevent.
            async for chunk in upstream.aiter_raw():
                yield chunk
            completed = True
        finally:
            # Runs on client disconnect too — GeneratorExit and CancelledError
            # both unwind through here — which is what cancels the upstream
            # generation.
            await upstream.aclose()
            if completed:
                log.debug("relay to %s closed", url)
            else:
                # INFO, not DEBUG: an abort is a real, rare event, and at the
                # default level it was previously invisible — which made
                # acceptance L9 impossible to confirm by reading logs.
                log.info("relay to %s aborted by the client; upstream cancelled", url)


async def _buffered_response(upstream: httpx.Response) -> JSONResponse:
    """Non-streaming: the OpenAI object verbatim, whatever it says."""
    try:
        await upstream.aread()
        payload = upstream.json()
    except ValueError:
        payload = {"error": {"code": "internal", "message": "upstream sent a non-JSON body"}}
    finally:
        await upstream.aclose()
    return JSONResponse(
        payload,
        status_code=upstream.status_code,
        headers=_forwarded_response_headers(upstream),
    )


async def _upstream_error_response(upstream: httpx.Response) -> JSONResponse:
    try:
        await upstream.aread()
        detail = upstream.text[:500]
    finally:
        await upstream.aclose()
    status = 502 if upstream.status_code >= 500 else upstream.status_code
    return AppError(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        "the model backend rejected the request",
        details={"upstream_status": upstream.status_code, "upstream_body": detail},
        http_status=status,
    ).response()


def _forwarded_request_headers(request: Request, supervisor: Supervisor) -> dict[str, str]:
    """Pass the client's content type through; never pass our bearer token upstream.

    The upstream's *own* credentials are a different thing and do go: they come
    from `supervisor.upstream_headers()`, which is `{}` for a local engine and the
    provider's bearer token for `remote_openai`. Ours would be meaningless there
    and handing it over would leak it.
    """
    headers = {"Content-Type": "application/json", "Accept": request.headers.get("accept", "*/*")}
    client_id = request.headers.get("x-harness-client")
    if client_id:
        headers["X-Harness-Client"] = client_id
    headers.update(supervisor.upstream_headers())
    return headers


def _forwarded_response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }
