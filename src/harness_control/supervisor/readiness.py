"""Polling an HTTP endpoint until it answers — the shape of "is it up yet?".

Shared by the backends that have something to wait for. Kept out of the backends
themselves so the retry/backoff behaviour is identical everywhere and testable on
its own.
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx


async def poll_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout_s: float,
    interval_s: float = 2.0,
) -> bool:
    """Call `predicate` every `interval_s` until it is true or `timeout_s` elapses.

    Returns whether it became true. The predicate is always called at least once,
    so a zero timeout still means "check now", not "give up immediately".
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if await predicate():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(min(interval_s, max(0.0, deadline - loop.time())))


async def http_ok(client: httpx.AsyncClient, url: str, *, timeout_s: float = 2.0) -> bool:
    """True when `url` answers 2xx. Any transport failure is just "not ready"."""
    try:
        response = await client.get(url, timeout=timeout_s)
    except httpx.HTTPError:
        return False
    return response.is_success
