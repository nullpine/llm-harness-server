"""`/admin/*` — the control surface the desktop app drives (contract §3).

Every route here is cheap or asynchronous. The desktop app polls `/admin/state`
at 2 s during an activation, and `POST .../activate` returns a 202 immediately
rather than holding the request open for the length of a load — an admin route
that blocks for a minute is indistinguishable from a dead server.
"""

import logging

from fastapi import APIRouter, Depends, Request, Response

from harness_control.auth import require_api_key
from harness_control.errors import AppError, ErrorCode
from harness_control.logbuf import MAX_REQUESTABLE_LINES
from harness_control.logging_config import log_buffer
from harness_control.models import (
    ActivateRequest,
    CatalogEntry,
    CatalogResponse,
    JobResponse,
    LogSource,
    LogsResponse,
    StateResponse,
)
from harness_control.supervisor.state import ModelState
from harness_control.supervisor.supervisor import (
    ActivationInProgressError,
    Supervisor,
    UnknownModelError,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_api_key)])


def _supervisor(request: Request) -> Supervisor:
    supervisor: Supervisor = request.app.state.supervisor
    return supervisor


@router.get("/state", response_model=StateResponse)
async def get_state(request: Request) -> StateResponse:
    return await _supervisor(request).state_payload()


@router.get("/models", response_model=CatalogResponse)
async def get_models(request: Request) -> CatalogResponse:
    """The full catalog, annotated with live state.

    `available` means the backend can serve it without first fetching it — for
    Ollama, that the tag is pulled. Deliberately not "is it loaded": every
    non-active model would then read as unavailable, and the dropdown would
    offer nothing to switch to.
    """
    supervisor = _supervisor(request)
    active = supervisor.active_model_id

    rows: list[CatalogEntry] = []
    for spec in supervisor.catalog.specs:
        rows.append(
            CatalogEntry(
                id=spec.id,
                display_name=spec.display_name,
                model_ref=spec.model_ref,
                params=spec.params,
                quantization=spec.quantization,
                context_length=spec.context_length,
                available=await supervisor.is_available(spec),
                # Only the active model carries the live state; the rest are idle
                # by definition, because only one can be active at a time.
                state=supervisor.state if spec.id == active else ModelState.IDLE,
                estimated_load_seconds=spec.estimated_load_seconds,
            )
        )

    return CatalogResponse(active_model_id=active, state=supervisor.state, models=rows)


@router.post("/models/{model_id}/activate")
async def activate_model(model_id: str, request: Request) -> Response:
    """Start a switch. 202 with a job, or 200 if it is already the active model."""
    supervisor = _supervisor(request)

    # The body is optional; `force` is accepted and currently unused — the drain
    # is short and skipping it would only risk cutting off a live reply.
    try:
        raw = await request.body()
        if raw:
            ActivateRequest.model_validate_json(raw)
    except ValueError as exc:
        raise AppError(ErrorCode.BAD_REQUEST, f"invalid body: {exc}") from exc

    try:
        job = await supervisor.activate(model_id)
    except UnknownModelError as exc:
        raise AppError(ErrorCode.NOT_FOUND, str(exc)) from exc
    except ActivationInProgressError as exc:
        raise AppError(ErrorCode.ACTIVATION_IN_PROGRESS, str(exc)) from exc

    if job is None:
        return _json(200, {"job_id": None, "model_id": model_id, "already_active": True})

    return _json(
        202,
        {
            "job_id": job.job_id,
            "model_id": model_id,
            "estimated_seconds": supervisor.estimated_seconds(model_id),
        },
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, request: Request) -> JobResponse:
    job = _supervisor(request).jobs.get(job_id)
    if job is None:
        raise AppError(ErrorCode.NOT_FOUND, f"no job {job_id!r}")
    return JobResponse(
        job_id=job.job_id,
        model_id=job.model_id,
        status=job.status,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        log_tail=job.log_tail,
    )


@router.get("/logs", response_model=LogsResponse)
async def get_logs(lines: int = 200, source: LogSource = "vllm") -> LogsResponse:
    """The tail of the ring buffer, for the desktop app's failure-diagnosis modal."""
    capped = max(1, min(lines, MAX_REQUESTABLE_LINES))
    # The control plane's own lifecycle, on every backend. Previously this read a
    # buffer fed only by the vLLM stdout pump, so on the Ollama path the desktop
    # app's "View server logs" — offered exactly when an activation fails — had
    # nothing to show.
    return LogsResponse(source=source, lines=log_buffer().tail(capped))


def _json(status: int, payload: dict[str, object]) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(payload, status_code=status)
