"""Activation: the state machine driven by a real (fake) backend.

The supervisor owns *when*; the backend owns *how*. These tests are about the
"when": which transitions happen, what the lock does, and what a failure leaves
behind for the client to read.
"""

import asyncio
import re

import pytest

from conftest import MODEL_ID
from fake_upstream import DEFAULT_MODEL, FakeUpstream
from harness_control.supervisor.state import ModelState
from harness_control.supervisor.supervisor import (
    ActivationInProgressError,
    Supervisor,
    UnknownModelError,
)


async def test_a_fresh_supervisor_is_idle(supervisor: Supervisor) -> None:
    assert supervisor.state is ModelState.IDLE
    assert supervisor.active_model_id is None
    assert supervisor.base_url() is None


async def test_activation_reaches_ready(supervisor: Supervisor, upstream: FakeUpstream) -> None:
    upstream.loaded.add(DEFAULT_MODEL)
    job = await supervisor.activate(MODEL_ID)

    assert job.status == "succeeded"
    assert supervisor.state is ModelState.READY
    assert supervisor.active_model_id == MODEL_ID
    assert supervisor.base_url() is not None


async def test_activation_preloads_through_the_backend(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """The supervisor must not talk to the engine itself — that is the backend's job."""
    upstream.loaded.add(DEFAULT_MODEL)
    await supervisor.activate(MODEL_ID)
    assert upstream.generate_calls == [{"model": DEFAULT_MODEL, "keep_alive": -1}]


async def test_an_unknown_model_is_rejected_before_anything_happens(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    with pytest.raises(UnknownModelError, match=re.escape("qwen3.8-27b")):
        await supervisor.activate("qwen3.8-27b")
    assert supervisor.state is ModelState.IDLE
    assert upstream.generate_calls == []
    assert len(supervisor.jobs) == 0, "a rejected id must not create a job"


async def test_a_backend_failure_lands_in_error_with_a_reason(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    upstream.generate_status = 500
    job = await supervisor.activate(MODEL_ID)

    assert job.status == "failed"
    assert supervisor.state is ModelState.ERROR
    assert supervisor.active_model_id is None

    payload = await supervisor.state_payload()
    assert payload.last_error is not None
    assert "500" in payload.last_error
    assert job.error == payload.last_error


async def test_a_model_that_never_becomes_ready_times_out(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """The daemon accepted the preload but the model never appeared in /api/ps."""
    supervisor._settings.load_timeout_s = 0.2  # private on purpose: the point of the test
    upstream.pin_on_generate = False

    job = await supervisor.activate(MODEL_ID)

    assert job.status == "failed"
    assert supervisor.state is ModelState.ERROR
    assert "not ready" in (job.error or "")


async def test_a_failure_does_not_crash_loop(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """ADR-0005: a crashed model stays in error until a client activates something."""
    upstream.generate_status = 500
    await supervisor.activate(MODEL_ID)
    await asyncio.sleep(0.1)
    assert supervisor.state is ModelState.ERROR
    assert len(upstream.generate_calls) == 1, "the supervisor retried on its own"


async def test_error_can_be_recovered_by_activating_again(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    upstream.generate_status = 500
    await supervisor.activate(MODEL_ID)
    # Read into locals: narrowing a property across two activations would make
    # the second assertion unreachable to the type checker.
    failed_state = supervisor.state
    assert failed_state is ModelState.ERROR

    upstream.generate_status = 200
    upstream.loaded.add(DEFAULT_MODEL)
    job = await supervisor.activate(MODEL_ID)

    assert job.status == "succeeded"
    recovered_state = supervisor.state
    assert recovered_state is ModelState.READY
    assert (await supervisor.state_payload()).last_error is None


async def test_a_concurrent_activation_is_rejected(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """Acceptance L10. The lock is non-blocking: the second caller is told, not queued."""
    upstream.loaded.add(DEFAULT_MODEL)
    upstream.generate_delay_s = 0.5

    first = asyncio.create_task(supervisor.activate(MODEL_ID))
    await asyncio.sleep(0.1)

    with pytest.raises(ActivationInProgressError):
        await supervisor.activate(MODEL_ID)

    job = await first
    assert job.status == "succeeded"


async def test_the_lock_is_released_after_a_failure(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """A lock leaked on the error path would wedge the server until a restart."""
    upstream.generate_status = 500
    await supervisor.activate(MODEL_ID)

    upstream.generate_status = 200
    upstream.loaded.add(DEFAULT_MODEL)
    assert (await supervisor.activate(MODEL_ID)).status == "succeeded"


async def test_shutdown_unloads_the_model(supervisor: Supervisor, upstream: FakeUpstream) -> None:
    """Leaving weights pinned after the control plane exits is how memory leaks."""
    upstream.loaded.add(DEFAULT_MODEL)
    await supervisor.activate(MODEL_ID)
    await supervisor.shutdown()

    assert upstream.generate_calls[-1] == {"model": DEFAULT_MODEL, "keep_alive": 0}
    assert DEFAULT_MODEL not in upstream.loaded


async def test_shutdown_is_safe_when_nothing_is_loaded(supervisor: Supervisor) -> None:
    await supervisor.shutdown()
    await supervisor.shutdown()


# ------------------------------------------------------------------- jobs


async def test_a_job_records_the_activation(supervisor: Supervisor, upstream: FakeUpstream) -> None:
    upstream.loaded.add(DEFAULT_MODEL)
    job = await supervisor.activate(MODEL_ID)

    assert job.job_id.startswith("act_")
    assert job.model_id == MODEL_ID
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.is_finished
    assert supervisor.jobs.get(job.job_id) is job


async def test_job_ids_are_unique(supervisor: Supervisor, upstream: FakeUpstream) -> None:
    upstream.loaded.add(DEFAULT_MODEL)
    ids = {(await supervisor.activate(MODEL_ID)).job_id for _ in range(3)}
    assert len(ids) == 3


def test_the_job_registry_is_bounded() -> None:
    """An unbounded registry is a slow memory leak on a long-running server."""
    from harness_control.supervisor.jobs import JobRegistry

    registry = JobRegistry(max_jobs=3)
    jobs = [registry.create("m") for _ in range(5)]
    assert len(registry) == 3
    assert registry.get(jobs[0].job_id) is None
    assert registry.get(jobs[-1].job_id) is not None


# ------------------------------------------------------------- state payload


async def test_the_state_payload_matches_the_contract(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    upstream.loaded.add(DEFAULT_MODEL)
    await supervisor.activate(MODEL_ID)
    payload = await supervisor.state_payload()

    assert payload.state is ModelState.READY
    assert payload.active_model_id == MODEL_ID
    assert payload.since.endswith("Z")
    assert payload.gpu == [], "no nvidia-smi here — L11"
    assert payload.progress_hint is None, "M1 does not parse pull progress"


async def test_estimated_seconds_comes_from_the_catalog(supervisor: Supervisor) -> None:
    assert supervisor.estimated_seconds(MODEL_ID) == 25
    assert supervisor.estimated_seconds("no-such-model") == 0
