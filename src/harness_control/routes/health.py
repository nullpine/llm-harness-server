"""`GET /healthz` — unauthenticated, and deliberately boring.

Used by Caddy and Azure health probes, so it must answer before a model exists and
must never reveal model or key information (contract §3).
"""

import time

from fastapi import APIRouter, Request

from harness_control.models import HealthResponse

#: The API contract version this server implements. Must equal the version in the
#: title of `docs/API-CONTRACT.md`; `tests/test_toolchain.py` asserts it, so the
#: doc and the code cannot drift apart the way they did between v1 and v1.1.
#: Bumping this is a two-repo change — see CLAUDE.md ground rule 3.
CONTRACT_VERSION = "1.1"

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    started_at: float = getattr(request.app.state, "started_at", time.monotonic())
    service_version: str = getattr(request.app.state, "version", "0.0.0")
    return HealthResponse(
        status="ok",
        version=CONTRACT_VERSION,
        service_version=service_version,
        uptime_s=int(time.monotonic() - started_at),
    )
