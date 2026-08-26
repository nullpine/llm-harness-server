"""Client disconnect must cancel the upstream generation.

Acceptance L9, and a direct cost concern: an orphaned generation burns GPU time on
a $7/hour machine with nobody reading the output. On the local Ollama path it
holds the model busy instead, which is just as user-visible.

The mechanism is `await resp.aclose()` in the relay's `finally` — when the client
hangs up, the `StreamingResponse` generator is closed, the `finally` runs, and
httpx tears down the upstream connection. The fake upstream notices its `send`
failing and sets `stream_aborted`.
"""

import asyncio
import time

import httpx
import pytest

from conftest import AUTH, MODEL_ID, LiveServer
from fake_upstream import FakeUpstream, sse_frames

#: Long enough that the stream is unambiguously still in flight when we hang up.
LONG_STREAM = [f"token-{i} " for i in range(200)]
ABORT_BUDGET_S = 2.0

BODY = {
    "model": MODEL_ID,
    "stream": True,
    "messages": [{"role": "user", "content": "Count to two hundred."}],
}


@pytest.fixture
def long_upstream(upstream: FakeUpstream) -> FakeUpstream:
    upstream.frames = sse_frames(LONG_STREAM)
    upstream.frame_delay_s = 0.05
    return upstream


async def test_disconnect_cancels_the_upstream_request(
    live_app: LiveServer, long_upstream: FakeUpstream
) -> None:
    """The L9 test: hang up mid-stream, the upstream learns about it quickly."""
    started = time.perf_counter()

    async with (
        httpx.AsyncClient(timeout=10.0) as client,
        client.stream(
            "POST", f"{live_app.url}/v1/chat/completions", json=BODY, headers=AUTH
        ) as response,
    ):
        assert response.status_code == 200
        async for _chunk in response.aiter_raw():
            break  # read one frame, then walk away

    await asyncio.wait_for(long_upstream.stream_aborted.wait(), timeout=ABORT_BUDGET_S)

    elapsed = time.perf_counter() - started
    assert elapsed < ABORT_BUDGET_S + 1.0
    assert long_upstream.frames_sent < len(long_upstream.frames), (
        "the upstream ran to completion even though the client had gone"
    )


async def test_a_cancelled_task_also_cancels_upstream(
    live_app: LiveServer, long_upstream: FakeUpstream
) -> None:
    """The other way a client goes away: the reader task is cancelled outright."""

    async def read_forever() -> None:
        async with (
            httpx.AsyncClient(timeout=10.0) as client,
            client.stream(
                "POST", f"{live_app.url}/v1/chat/completions", json=BODY, headers=AUTH
            ) as response,
        ):
            async for _chunk in response.aiter_raw():
                pass

    task = asyncio.create_task(read_forever())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(long_upstream.stream_aborted.wait(), timeout=ABORT_BUDGET_S)


async def test_the_server_survives_the_disconnect(
    live_app: LiveServer, long_upstream: FakeUpstream
) -> None:
    """An abort must not take the control plane — or the next request — with it."""
    async with (
        httpx.AsyncClient(timeout=10.0) as client,
        client.stream(
            "POST", f"{live_app.url}/v1/chat/completions", json=BODY, headers=AUTH
        ) as response,
    ):
        async for _chunk in response.aiter_raw():
            break

    await asyncio.wait_for(long_upstream.stream_aborted.wait(), timeout=ABORT_BUDGET_S)

    async with httpx.AsyncClient(timeout=5.0) as client:
        health = await client.get(f"{live_app.url}/healthz")
        state = await client.get(f"{live_app.url}/admin/state", headers=AUTH)
    assert health.status_code == 200
    assert state.status_code == 200
    assert state.json()["state"] == "ready"


async def test_a_second_stream_works_after_an_abort(
    live_app: LiveServer, long_upstream: FakeUpstream
) -> None:
    """A leaked upstream connection would show up here as a hang or a pool timeout."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        async with client.stream(
            "POST", f"{live_app.url}/v1/chat/completions", json=BODY, headers=AUTH
        ) as response:
            async for _chunk in response.aiter_raw():
                break

        long_upstream.frames = sse_frames(["done"])
        long_upstream.frame_delay_s = 0.0
        async with client.stream(
            "POST", f"{live_app.url}/v1/chat/completions", json=BODY, headers=AUTH
        ) as response:
            body = b"".join([chunk async for chunk in response.aiter_raw()])
    assert body.endswith(b"data: [DONE]\n\n")


def test_the_relay_closes_upstream_in_a_finally() -> None:
    """Structural: the `finally` is what makes disconnect propagate at all."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "harness_control" / "proxy.py"
    ).read_text()
    relay = source.split("async def _relay(", 1)[1].split("\nasync def ", 1)[0]
    assert "finally:" in relay
    assert relay.index("finally:") < relay.index("await upstream.aclose()")
