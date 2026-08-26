"""Frames must arrive spread out in time, not batched at the end.

This is the milestone (acceptance L3) and the single most likely bug in the whole
component. The failure is silent: a buffering layer still delivers every byte, in
the right order, with the right content — just all at once, after the model has
finished. A test that concatenates the body and compares strings passes happily
while streaming is completely broken.

So these tests assert **arrival times**. The fake upstream emits frames a fixed
interval apart and the client records when each one showed up.

They run against a real uvicorn socket (the `live_app` fixture), not
`ASGITransport`, because that transport buffers the whole body by construction and
would make every assertion here vacuous.
"""

import asyncio
import itertools
import re
import time
from pathlib import Path

import httpx
import pytest

from conftest import AUTH, MODEL_ID, LiveServer
from fake_upstream import FakeUpstream, sse_frames
from harness_control.supervisor.supervisor import Supervisor

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Big enough to be unmistakable against scheduler jitter, small enough to keep
#: the suite quick. Six frames at 100 ms is a ~500 ms spread.
FRAME_GAP_S = 0.1
WORDS = ["one ", "two ", "three ", "four ", "five "]


def body(stream: bool = True) -> dict[str, object]:
    return {
        "model": MODEL_ID,
        "stream": stream,
        "messages": [{"role": "user", "content": "Count slowly from 1 to 5."}],
    }


async def collect(
    url: str, *, headers: dict[str, str] | None = None, payload: dict[str, object] | None = None
) -> tuple[list[tuple[float, bytes]], httpx.Response]:
    """POST a completion and record `(elapsed_seconds, chunk)` for every raw chunk."""
    arrivals: list[tuple[float, bytes]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        started = time.perf_counter()
        async with client.stream(
            "POST",
            f"{url}/v1/chat/completions",
            json=payload or body(),
            headers={**AUTH, **(headers or {})},
        ) as response:
            async for chunk in response.aiter_raw():
                if chunk:
                    arrivals.append((time.perf_counter() - started, chunk))
            return arrivals, response


@pytest.fixture
def slow_upstream(upstream: FakeUpstream) -> FakeUpstream:
    upstream.frames = sse_frames(WORDS)
    upstream.frame_delay_s = FRAME_GAP_S
    return upstream


# ------------------------------------------------------------ the real test


async def test_frames_arrive_incrementally_not_batched(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    """The one that matters. If this fails, do not merge — something buffers."""
    arrivals, response = await collect(live_app.url)

    assert response.status_code == 200
    assert len(arrivals) >= 2, "the whole response arrived as a single chunk"

    first, last = arrivals[0][0], arrivals[-1][0]
    expected_span = FRAME_GAP_S * (len(slow_upstream.frames) - 1)

    # Half the upstream's own spread: generous against a loaded CI box, and still
    # impossible to pass if the response was assembled and sent at the end.
    assert last - first >= expected_span * 0.5, (
        f"all {len(arrivals)} chunks arrived within {last - first:.3f}s of each other, "
        f"but the upstream spread them over {expected_span:.3f}s — something is buffering"
    )


async def test_the_first_frame_arrives_promptly(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    """L3: the first `data:` frame within 3 s."""
    arrivals, _ = await collect(live_app.url)
    assert arrivals[0][0] < 3.0, f"first frame took {arrivals[0][0]:.3f}s"
    assert arrivals[0][1].startswith(b"data: ")


async def test_the_client_sees_the_gap_between_frames(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    """Not just "spread out overall" — consecutive frames are actually separated."""
    arrivals, _ = await collect(live_app.url)
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(arrivals)]
    assert gaps, "only one chunk arrived"
    assert max(gaps) >= FRAME_GAP_S * 0.5, (
        f"largest inter-chunk gap was {max(gaps):.3f}s; frames were coalesced"
    )


async def test_the_first_frame_is_not_held_back_by_later_ones(
    live_app: LiveServer, upstream: FakeUpstream
) -> None:
    """A frame now, then a long pause, then the rest.

    A buffering proxy delivers nothing until the pause is over. A streaming one
    hands the first frame over immediately — which is what makes a chat UI feel
    responsive rather than frozen.
    """
    upstream.frames = [*sse_frames(["hello"])[:1], *sse_frames(["world"])]
    upstream.frame_delay_s = 0.6

    arrivals, _ = await collect(live_app.url)
    assert arrivals[0][0] < 0.5, (
        f"the first frame waited {arrivals[0][0]:.3f}s for frames behind it"
    )


# -------------------------------------------------------------- the content


async def test_the_frames_are_relayed_verbatim(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    """Contract §2: SSE frames pass through unchanged, ending with `data: [DONE]`."""
    arrivals, _ = await collect(live_app.url)
    received = b"".join(chunk for _, chunk in arrivals)
    assert received == b"".join(slow_upstream.frames)
    assert received.endswith(b"data: [DONE]\n\n")
    for word in WORDS:
        assert word.strip().encode() in received


async def test_reasoning_content_passes_through(
    live_app: LiveServer, upstream: FakeUpstream
) -> None:
    """Reasoning models emit `delta.reasoning_content`; we must not filter it."""
    upstream.frames = [
        b'data: {"choices":[{"delta":{"reasoning_content":"thinking"},"index":0}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    arrivals, _ = await collect(live_app.url)
    assert b"reasoning_content" in b"".join(chunk for _, chunk in arrivals)


# -------------------------------------------------------------- the headers


async def test_the_anti_buffering_headers_are_set(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    _, response = await collect(live_app.url)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


async def test_the_response_is_not_content_length_delimited(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    """A `Content-Length` means the body was complete before it was sent."""
    _, response = await collect(live_app.url)
    assert "content-length" not in response.headers


async def test_the_response_is_never_compressed(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    """Even when the client asks for it. Compression implies buffering."""
    _, response = await collect(live_app.url, headers={"Accept-Encoding": "gzip, deflate, br"})
    assert "content-encoding" not in response.headers


async def test_it_still_streams_when_the_client_offers_gzip(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    arrivals, _ = await collect(live_app.url, headers={"Accept-Encoding": "gzip"})
    assert arrivals[-1][0] - arrivals[0][0] >= FRAME_GAP_S


# ------------------------------------------------ the other three defences


def test_the_app_has_no_compression_middleware() -> None:
    """Defence 2, asserted structurally rather than by hoping nobody adds it."""
    from harness_control.app import create_app
    from harness_control.settings import Settings

    fastapi_app = create_app(Settings(), supervisor=_null_supervisor())
    stack = [getattr(m.cls, "__name__", str(m.cls)) for m in fastapi_app.user_middleware]
    assert not any("GZip" in name or "Brotli" in name for name in stack), stack


def _null_supervisor() -> "Supervisor":
    from harness_control.catalog import Catalog
    from harness_control.settings import Settings

    return Supervisor(Catalog([]), Settings())


def test_the_relay_uses_aiter_raw_and_nothing_else() -> None:
    """Defence 1. `aiter_text`/`aiter_lines`/`.json()` on the relay path all buffer."""
    source = (REPO_ROOT / "src" / "harness_control" / "proxy.py").read_text()
    relay = source.split("async def _relay(", 1)[1].split("\nasync def ", 1)[0]
    assert "aiter_raw()" in relay
    # The call forms, not the words: the prose in this module names them all.
    for forbidden in (".aiter_text(", ".aiter_lines(", ".aiter_bytes(", ".json()"):
        assert forbidden not in relay, f"{forbidden} on the relay path buffers"


def test_the_streaming_route_declares_no_response_model() -> None:
    """A response_model would validate — and therefore materialise — the body."""
    source = (REPO_ROOT / "src" / "harness_control" / "routes" / "openai.py").read_text()
    decorator = source.split('@router.post("/chat/completions"', 1)[1].split(")", 1)[0]
    assert "response_model" not in decorator


def test_caddy_disables_buffering_on_the_streaming_path() -> None:
    """Defence 3. Caddy is not in the local path, but it is in the deployed one."""
    caddyfile = (REPO_ROOT / "deploy" / "caddy" / "Caddyfile.template").read_text()
    stream_block = caddyfile.split("@stream", 1)[1].split("@rest", 1)[0]
    assert "flush_interval -1" in stream_block
    assert "127.0.0.1:8080" in stream_block

    encode_lines = [
        line.strip() for line in caddyfile.splitlines() if line.strip().startswith("encode")
    ]
    assert encode_lines, "expected an encode directive somewhere"
    for line in encode_lines:
        assert re.match(r"encode\s+@\w+", line), (
            f"unmatched `{line}` compresses /v1/* and buffers the stream"
        )


# -------------------------------------------------------- the non-streaming path


async def test_non_streaming_still_returns_the_openai_object(
    live_app: LiveServer, upstream: FakeUpstream
) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{live_app.url}/v1/chat/completions", json=body(stream=False), headers=AUTH
        )
    assert response.status_code == 200
    assert response.json() == upstream.non_stream_body


async def test_a_slow_upstream_does_not_block_other_requests(
    live_app: LiveServer, slow_upstream: FakeUpstream
) -> None:
    """No blocking calls in the async path: a stream in flight must not stall /healthz."""
    stream_task = asyncio.create_task(collect(live_app.url))
    await asyncio.sleep(FRAME_GAP_S)

    async with httpx.AsyncClient(timeout=5.0) as client:
        started = time.perf_counter()
        health = await client.get(f"{live_app.url}/healthz")
        elapsed = time.perf_counter() - started

    assert health.status_code == 200
    assert elapsed < 1.0, f"/healthz waited {elapsed:.3f}s behind an in-flight stream"
    await stream_task
