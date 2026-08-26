"""`GET /healthz` — unauthenticated, and deliberately boring.

Used by Caddy and Azure health probes, so it must answer before a model exists and
must never reveal model or key information (contract §3).
"""

import time

from fastapi import APIRouter, Request

from harness_control.models import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    started_at: float = getattr(request.app.state, "started_at", time.monotonic())
    version: str = getattr(request.app.state, "version", "0.0.0")
    return HealthResponse(
        status="ok",
        version=version,
        uptime_s=int(time.monotonic() - started_at),
    )
