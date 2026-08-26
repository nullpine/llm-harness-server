"""The OpenAI-compatible surface: `GET /v1/models`, `POST /v1/chat/completions`.

Note what these routes do *not* do: validate or re-serialise the completion
response. The contract says the body is vLLM's object verbatim, and a
`response_model` on the streaming route would buffer the stream
(`.claude/rules/streaming.md` §2).
"""

import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response

from harness_control.auth import require_api_key
from harness_control.errors import AppError, ErrorCode
from harness_control.models import ModelCard, ModelList
from harness_control.proxy import guard_state, relay_chat_completions
from harness_control.supervisor.state import ModelState
from harness_control.supervisor.supervisor import Supervisor

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def _supervisor(request: Request) -> Supervisor:
    supervisor: Supervisor = request.app.state.supervisor
    return supervisor


def _http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http_client
    return client


@router.get("/models", response_model=ModelList)
async def list_models(request: Request) -> ModelList:
    """Only the model you can call right now. `/admin/models` has the catalog."""
    supervisor = _supervisor(request)
    active = supervisor.active_model_id
    if active is None or supervisor.state is not ModelState.READY:
        return ModelList(data=[])
    return ModelList(data=[ModelCard(id=active, created=int(time.time()))])


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Guard, then relay. Everything interesting is in `proxy.py`."""
    body = await _json_body(request)
    requested = body.get("model")
    if not isinstance(requested, str) or not requested:
        raise AppError(ErrorCode.BAD_REQUEST, "`model` is required and must be a string")
    if not isinstance(body.get("messages"), list):
        raise AppError(ErrorCode.BAD_REQUEST, "`messages` is required and must be a list")

    supervisor = _supervisor(request)
    guard_state(supervisor, requested)
    return await relay_chat_completions(request, supervisor, _http_client(request), body)


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse by hand, so unknown fields reach the backend unchanged (contract §2)."""
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise AppError(ErrorCode.BAD_REQUEST, f"request body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AppError(ErrorCode.BAD_REQUEST, "request body must be a JSON object")
    return payload
