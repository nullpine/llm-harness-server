"""A configurable stand-in for whatever is serving the model.

Replaces the planned `fake_vllm.py`: with three backends there is no single "the
upstream" any more. One app serves all of it — Ollama's `/api/*`, vLLM's
`/health`, and the OpenAI routes both of them expose.

Written as a raw ASGI app rather than a FastAPI one on purpose. The whole point of
`test_proxy_streaming.py` is to catch a layer that buffers; a test fixture that
might itself buffer would make the result meaningless.

Also runnable as a script, so `tests/test_backends.py` can spawn it as a fake
`vllm` binary and exercise the real process-group spawn and kill path:

    python tests/fake_upstream.py serve <model> --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

DEFAULT_MODEL = "glm-4.7-flash:q4_K_M"
SECOND_MODEL = "qwen3.8:27b-q4_K_M"


def sse_frames(words: list[str], *, completion_id: str = "chatcmpl-fake") -> list[bytes]:
    """The frames a real OpenAI-compatible server would emit for `words`."""
    frames = [
        b"data: "
        + json.dumps(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "choices": [{"delta": {"content": word}, "index": 0}],
            }
        ).encode()
        + b"\n\n"
        for word in words
    ]
    frames.append(
        b"data: "
        + json.dumps(
            {
                "id": completion_id,
                "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
                "usage": {"prompt_tokens": 4, "completion_tokens": len(words)},
            }
        ).encode()
        + b"\n\n"
    )
    frames.append(b"data: [DONE]\n\n")
    return frames


@dataclass
class FakeUpstream:
    """The ASGI app plus every knob a test needs to turn.

    Mutate the fields between requests; nothing is cached.
    """

    # --- what the model routes do -----------------------------------------
    frames: list[bytes] = field(default_factory=lambda: sse_frames(["Hel", "lo", "!"]))
    frame_delay_s: float = 0.0
    first_frame_delay_s: float = 0.0
    chat_status: int = 200
    chat_error_body: dict[str, Any] = field(default_factory=lambda: {"error": "upstream said no"})
    non_stream_body: dict[str, Any] = field(
        default_factory=lambda: {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
        }
    )

    # --- what the control routes say --------------------------------------
    loaded: set[str] = field(default_factory=set)
    #: Tags pulled to disk, which `/api/tags` reports. Distinct from `loaded`:
    #: a model can be available without being resident, which is exactly the
    #: distinction `/admin/models` depends on.
    pulled: set[str] = field(default_factory=lambda: {DEFAULT_MODEL, SECOND_MODEL})
    #: How long an unload takes to actually show up in `/api/ps`. Non-zero
    #: exercises the supervisor's wait-for-release guard.
    unload_delay_s: float = 0.0
    served_models: set[str] = field(default_factory=lambda: {DEFAULT_MODEL})
    healthy: bool = True
    generate_status: int = 200
    #: When False, `/api/generate` succeeds but the model never shows up in
    #: `/api/ps` — the daemon accepted the preload and then never finished it.
    pin_on_generate: bool = True
    ps_status: int = 200
    models_status: int = 200
    generate_delay_s: float = 0.0

    # --- what actually happened -------------------------------------------
    generate_calls: list[dict[str, Any]] = field(default_factory=list)
    chat_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Set when a streaming response was cut short — the abort-propagation signal.
    stream_aborted: asyncio.Event = field(default_factory=asyncio.Event)
    frames_sent: int = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        path = scope["path"]
        method = scope["method"]
        if path == "/api/generate" and method == "POST":
            await self._api_generate(receive, send)
        elif path == "/api/ps":
            await self._json(
                send, self.ps_status, {"models": [{"name": n} for n in sorted(self.loaded)]}
            )
        elif path == "/api/tags":
            await self._json(
                send,
                self.ps_status,
                {"models": [{"name": n, "model": n} for n in sorted(self.pulled)]},
            )
        elif path == "/api/version":
            await self._json(send, 200, {"version": "0.0.0-fake"})
        elif path == "/health":
            await self._json(send, 200 if self.healthy else 503, {"status": "ok"})
        elif path == "/v1/models":
            await self._json(
                send,
                self.models_status,
                {
                    "object": "list",
                    "data": [{"id": m, "object": "model"} for m in sorted(self.served_models)],
                },
            )
        elif path == "/v1/chat/completions" and method == "POST":
            await self._chat(scope, receive, send)
        else:
            await self._json(send, 404, {"error": f"fake upstream has no {method} {path}"})

    # ------------------------------------------------------------ handlers

    async def _api_generate(self, receive: Receive, send: Send) -> None:
        """Ollama's preload/unload call: `keep_alive: -1` loads, `0` unloads."""
        body = await _read_body(receive)
        payload = json.loads(body) if body else {}
        self.generate_calls.append(payload)
        if self.generate_delay_s:
            await asyncio.sleep(self.generate_delay_s)
        if self.generate_status != 200:
            await self._json(send, self.generate_status, {"error": "generate refused"})
            return
        model = payload.get("model")
        keep_alive = payload.get("keep_alive")
        if isinstance(model, str):
            if keep_alive == 0:
                if self.unload_delay_s:
                    # Ollama's unload is asynchronous; `keep_alive: 0` only asks.
                    asyncio.get_running_loop().call_later(
                        self.unload_delay_s, self.loaded.discard, model
                    )
                else:
                    self.loaded.discard(model)
            elif self.pin_on_generate:
                self.loaded.add(model)
        await self._json(send, 200, {"model": model, "done": True})

    async def _chat(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = await _read_body(receive)
        payload = json.loads(body) if body else {}
        self.chat_calls.append({"body": payload, "headers": dict(scope.get("headers") or [])})

        if self.chat_status != 200:
            await self._json(send, self.chat_status, self.chat_error_body)
            return
        if not payload.get("stream"):
            await self._json(send, 200, self.non_stream_body)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-cache"),
                ],
            }
        )
        self.frames_sent = 0
        # uvicorn's HTTP `send()` silently no-ops once the peer is gone — it does
        # not raise and does not cancel. `receive()` is the only ASGI-level signal
        # that the client hung up, so watch it alongside the send loop. A real
        # engine does the same thing to stop generating tokens nobody will read.
        gone = asyncio.create_task(_wait_for_disconnect(receive))
        try:
            if self.first_frame_delay_s:
                await asyncio.sleep(self.first_frame_delay_s)
            for index, frame in enumerate(self.frames):
                if index and self.frame_delay_s:
                    await asyncio.wait({gone}, timeout=self.frame_delay_s)
                if gone.done():
                    raise _ClientGoneError
                await send({"type": "http.response.body", "body": frame, "more_body": True})
                self.frames_sent += 1
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except (_ClientGoneError, asyncio.CancelledError, OSError):
            # The client went away mid-stream. That is the whole point of abort
            # propagation, so record it.
            self.stream_aborted.set()
        finally:
            gone.cancel()

    # ------------------------------------------------------------- plumbing

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _json(self, send: Send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _ClientGoneError(Exception):
    """The peer hung up while we were still sending frames."""


async def _wait_for_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(bytes(message.get("body") or b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _main(argv: list[str]) -> int:
    """Run as a server, so this file can also stand in for the `vllm` binary."""
    import uvicorn

    host = "127.0.0.1"
    port = 8000
    for flag, setter in (("--host", "host"), ("--port", "port")):
        if flag in argv:
            value = argv[argv.index(flag) + 1]
            if setter == "host":
                host = value
            else:
                port = int(value)
    upstream = FakeUpstream()
    upstream.loaded.add(DEFAULT_MODEL)
    uvicorn.run(upstream, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
