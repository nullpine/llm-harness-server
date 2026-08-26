"""`/admin/*`. M1 ships `GET /admin/state` only; the rest lands in M2.

The desktop app polls `/admin/state` at 2 s during an activation and 30 s
otherwise, so it must stay cheap and must never fail — an admin route that 500s
while a load is in flight is indistinguishable to the client from a dead server.
"""

from fastapi import APIRouter, Depends, Request

from harness_control.auth import require_api_key
from harness_control.models import StateResponse
from harness_control.supervisor.supervisor import Supervisor

router = APIRouter(prefix="/admin", dependencies=[Depends(require_api_key)])


@router.get("/state", response_model=StateResponse)
async def get_state(request: Request) -> StateResponse:
    supervisor: Supervisor = request.app.state.supervisor
    return await supervisor.state_payload()
