"""Pydantic schemas for every request and response body in `docs/API-CONTRACT.md`.

Note what is *not* here: a schema for the OpenAI chat-completion response. The
contract says the response is "the OpenAI object verbatim from vLLM", and the
streaming route must not validate — a response model on `/v1/chat/completions`
would buffer the stream (`.claude/rules/streaming.md` §2).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness_control.supervisor.state import ModelState

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
LogSource = Literal["vllm", "control"]


# --------------------------------------------------------------- /healthz


class HealthResponse(BaseModel):
    """`GET /healthz`. Never reveals model or key information (contract §3).

    Two versions, and they are not interchangeable. `version` is the API contract
    version this server implements — the desktop app compares it for
    compatibility. `service_version` is the build, for debugging only; clients
    MUST NOT branch on it.
    """

    status: Literal["ok"] = "ok"
    version: str
    service_version: str
    uptime_s: int


# ------------------------------------------------------------- /v1/models


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    owned_by: Literal["harness"] = "harness"
    created: int


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard] = Field(default_factory=list)


# --------------------------------------------------- /v1/chat/completions


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class ChatCompletionRequest(BaseModel):
    """The subset the MVP understands. Unknown fields are forwarded unchanged, so
    `extra="allow"` is load-bearing, not laziness (contract §2)."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: str | list[str] | None = None


# ------------------------------------------------------------------ /admin


class GpuInfo(BaseModel):
    index: int
    name: str
    memory_used_mb: int
    memory_total_mb: int
    utilization_pct: int


class StateResponse(BaseModel):
    """`GET /admin/state` — cheap and pollable."""

    state: ModelState
    active_model_id: str | None = None
    previous_model_id: str | None = None
    since: str
    progress_hint: str | None = None
    last_error: str | None = None
    gpu: list[GpuInfo] = Field(default_factory=list)


class CatalogEntry(BaseModel):
    """One row of `GET /admin/models`: the catalog annotated with live state."""

    id: str
    display_name: str
    model_ref: str
    params: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    available: bool = False
    state: ModelState
    estimated_load_seconds: int


class CatalogResponse(BaseModel):
    active_model_id: str | None = None
    state: ModelState
    models: list[CatalogEntry] = Field(default_factory=list)


class ActivateRequest(BaseModel):
    force: bool = False


class ActivateAccepted(BaseModel):
    job_id: str
    model_id: str
    estimated_seconds: int


class ActivateAlreadyActive(BaseModel):
    job_id: None = None
    model_id: str
    already_active: Literal[True] = True


class JobResponse(BaseModel):
    job_id: str
    model_id: str
    status: JobStatus
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    log_tail: list[str] = Field(default_factory=list)


class LogsResponse(BaseModel):
    source: LogSource
    lines: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- the envelope


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody
