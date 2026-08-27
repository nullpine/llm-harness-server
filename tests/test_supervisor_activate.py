"""Switching: the transition, the drain, the unload guard, and the watchdog.

Acceptance L4, L5, L8, L10 and L11 live here; L6 and L7 are the proxy's guards
and live in `test_admin_routes.py`. Everything runs against `fake_upstream` with
no Ollama daemon anywhere.

The transition is the whole milestone. Both steady states were proved in M1; what
was never exercised is the path between them, and that is where the two findings
from M1 verification bite:

  * `stop()` does not make a model unreachable — Ollama reloads it on the next
    request — so the proxy's state guard is what enforces single-active.
  * The unload is asynchronous, so "stop() returned" is not "the model is gone".
"""

import asyncio

import pytest

from conftest import MODEL_ID, SECOND_MODEL_ID, activate_and_wait
from fake_upstream import DEFAULT_MODEL, SECOND_MODEL, FakeUpstream
from harness_control.supervisor.state import ModelState
from harness_control.supervisor.supervisor import (
    ActivationInProgressError,
    Supervisor,
    UnknownModelError,
)


async def settle(supervisor: Supervisor, timeout_s: float = 10.0) -> None:
    """Wait for whatever activation is running to finish.

    Awaits the task rather than polling the state: `activate()` returns before
    its background work starts, so the state is still the *old* one at that
    point and a state-poll would exit immediately.
    """
    await asyncio.wait_for(supervisor.wait_for_activation(), timeout=timeout_s)


# --- the basics ---------------------------------------------------------------


async def test_a_fresh_supervisor_is_idle(supervisor: Supervisor) -> None:
    assert supervisor.state is ModelState.IDLE
    assert supervisor.active_model_id is None
    assert supervisor.base_url() is None


async def test_activation_returns_before_the_load_finishes(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """Contract §3: activation is asynchronous and the client polls.

    Blocking here would make the 202 a lie and hold an admin request open for the
    length of a load.
    """
    upstream.loaded.add(DEFAULT_MODEL)
    upstream.generate_delay_s = 0.3

    job = await supervisor.activate(MODEL_ID)

    assert job is not None
    assert job.status == "running"
    assert supervisor.state is not ModelState.READY
    await settle(supervisor)


async def test_activation_reaches_ready(supervisor: Supervisor, upstream: FakeUpstream) -> None:
    upstream.loaded.add(DEFAULT_MODEL)
    await activate_and_wait(supervisor, MODEL_ID)

    assert supervisor.state is ModelState.READY
    assert supervisor.active_model_id == MODEL_ID
    assert supervisor.base_url() is not None


async def test_activation_preloads_through_the_backend(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """The supervisor must not talk to the engine itself — that is the backend's job."""
    upstream.loaded.add(DEFAULT_MODEL)
    await activate_and_wait(supervisor, MODEL_ID)

    assert upstream.generate_calls == [{"model": DEFAULT_MODEL, "keep_alive": -1}]


async def test_an_unknown_model_is_rejected_before_anything_happens(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    with pytest.raises(UnknownModelError, match="no-such-model"):
        await supervisor.activate("no-such-model")

    assert supervisor.state is ModelState.IDLE
    assert upstream.generate_calls == []
    assert len(supervisor.jobs) == 0, "a rejected id must not create a job"


async def test_activating_the_active_model_is_a_no_op(ready_supervisor: Supervisor) -> None:
    """Contract §3: already active and ready is a 200, not a pointless reload."""
    before = len(ready_supervisor.jobs)

    assert await ready_supervisor.activate(MODEL_ID) is None

    assert ready_supervisor.state is ModelState.READY
    assert len(ready_supervisor.jobs) == before, "a no-op must not create a job"


# --- L4: the switch -----------------------------------------------------------


async def test_l4_switching_moves_the_active_model(ready_supervisor: Supervisor) -> None:
    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)

    assert ready_supervisor.state is ModelState.READY
    assert ready_supervisor.active_model_id == SECOND_MODEL_ID


async def test_l4_the_switch_passes_through_stopping_and_loading(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """The states are reported honestly, not skipped straight to ready."""
    upstream.generate_delay_s = 0.2
    seen: list[ModelState] = []

    async def watch() -> None:
        while True:
            if not seen or seen[-1] is not ready_supervisor.state:
                seen.append(ready_supervisor.state)
            await asyncio.sleep(0.005)

    watcher = asyncio.create_task(watch())
    try:
        await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)
    finally:
        watcher.cancel()

    assert ModelState.STOPPING in seen
    assert ModelState.LOADING in seen


async def test_l4_the_old_model_is_unloaded(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)

    assert upstream.generate_calls[-2] == {"model": DEFAULT_MODEL, "keep_alive": 0}
    assert upstream.generate_calls[-1] == {"model": SECOND_MODEL, "keep_alive": -1}


async def test_l4_a_job_records_the_switch(ready_supervisor: Supervisor) -> None:
    job = await ready_supervisor.activate(SECOND_MODEL_ID)
    assert job is not None
    await settle(ready_supervisor)

    assert job.status == "succeeded"
    assert job.model_id == SECOND_MODEL_ID
    assert job.started_at is not None
    assert job.finished_at is not None
    assert ready_supervisor.jobs.get(job.job_id) is job


# --- L5: exactly one model resident, throughout ------------------------------


async def test_l5_only_one_model_is_ever_resident_during_a_switch(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """The check that catches this class of bug on hardware with RAM to spare.

    Both models fit in 48 GB, so an unload that never completed would not thrash
    — it would quietly leave two resident while `/admin/state` reports one active.
    Nothing else in the system would notice, which is why this samples `/api/ps`
    *throughout* the switch rather than only at the end.
    """
    upstream.generate_delay_s = 0.15
    upstream.unload_delay_s = 0.1

    samples: list[int] = []

    async def watch_residency() -> None:
        while True:
            samples.append(len(upstream.loaded))
            await asyncio.sleep(0.005)

    watcher = asyncio.create_task(watch_residency())
    try:
        await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)
    finally:
        watcher.cancel()

    assert samples, "the switch finished before residency was sampled"
    assert max(samples) <= 1, f"two models were resident at once: samples={samples}"
    assert upstream.loaded == {SECOND_MODEL}


async def test_l5_exactly_one_model_is_resident_after_a_switch(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    assert upstream.loaded == {DEFAULT_MODEL}

    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)

    assert upstream.loaded == {SECOND_MODEL}


async def test_l5_the_load_waits_for_the_unload_to_complete(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """`stop()` returning is not `the model is gone` — Ollama's unload is async."""
    upstream.unload_delay_s = 0.25

    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)

    assert upstream.loaded == {SECOND_MODEL}


async def test_l5_an_unload_that_never_completes_fails_the_activation(
    ready_supervisor: Supervisor, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better to refuse than to load on top of a model that is still resident."""
    from harness_control.supervisor import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "UNLOAD_TIMEOUT_S", 0.3)
    # The daemon acknowledges the unload and then never actually does it.
    upstream.unload_delay_s = 30.0

    job = await ready_supervisor.activate(SECOND_MODEL_ID)
    assert job is not None
    await settle(ready_supervisor, timeout_s=10.0)

    assert job.status == "failed"
    assert ready_supervisor.state is ModelState.ERROR
    assert "still loaded" in (job.error or "")
    # And crucially: it did not go on to load the new model anyway.
    assert SECOND_MODEL not in upstream.loaded


# --- the drain ----------------------------------------------------------------


async def test_the_drain_waits_for_in_flight_completions(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """A reply still streaming to a reader deserves to finish."""
    released = asyncio.Event()

    async def pretend_request() -> None:
        async with ready_supervisor.track_request():
            await released.wait()

    request = asyncio.create_task(pretend_request())
    await asyncio.sleep(0.02)
    assert ready_supervisor.in_flight == 1

    job = await ready_supervisor.activate(SECOND_MODEL_ID)
    assert job is not None
    await asyncio.sleep(0.1)

    # Still draining: the unload has not been asked for.
    draining_state = ready_supervisor.state
    assert draining_state is ModelState.STOPPING
    assert not any(call.get("keep_alive") == 0 for call in upstream.generate_calls)

    released.set()
    await request
    await settle(ready_supervisor)
    settled_state = ready_supervisor.state
    assert settled_state is ModelState.READY


async def test_the_drain_gives_up_rather_than_blocking_forever(
    ready_supervisor: Supervisor,
) -> None:
    """A hung stream must not hold the switch open indefinitely."""
    ready_supervisor._settings.drain_timeout_s = 0.2  # private on purpose
    forever = asyncio.Event()

    async def hung_request() -> None:
        async with ready_supervisor.track_request():
            await forever.wait()

    request = asyncio.create_task(hung_request())
    await asyncio.sleep(0.02)

    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID, timeout_s=10.0)
    assert ready_supervisor.state is ModelState.READY

    forever.set()
    await request


async def test_an_aborted_request_releases_the_drain(ready_supervisor: Supervisor) -> None:
    """The counter is released in `__aexit__`, so an abort frees the drain too."""

    async def aborted_request() -> None:
        async with ready_supervisor.track_request():
            raise RuntimeError("the client hung up")

    with pytest.raises(RuntimeError):
        await aborted_request()

    assert ready_supervisor.in_flight == 0


# --- L10: concurrent activation ----------------------------------------------


async def test_l10_a_second_activation_is_refused(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """Told, not queued: a client waiting behind a load has no way to know."""
    upstream.generate_delay_s = 0.3

    first = await ready_supervisor.activate(SECOND_MODEL_ID)
    assert first is not None

    with pytest.raises(ActivationInProgressError):
        await ready_supervisor.activate(MODEL_ID)

    await settle(ready_supervisor)
    assert ready_supervisor.active_model_id == SECOND_MODEL_ID


async def test_the_lock_is_released_after_a_failure(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """A lock leaked on the error path would wedge the server until a restart."""
    upstream.generate_status = 500
    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)
    # Read into a local: narrowing a property across two activations makes the
    # second assertion unreachable to the type checker.
    failed_state = ready_supervisor.state
    assert failed_state is ModelState.ERROR

    upstream.generate_status = 200
    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)
    recovered_state = ready_supervisor.state
    assert recovered_state is ModelState.READY


# --- failures -----------------------------------------------------------------


async def test_a_backend_failure_lands_in_error_with_a_reason(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    upstream.generate_status = 500
    await activate_and_wait(supervisor, MODEL_ID)

    assert supervisor.state is ModelState.ERROR
    assert supervisor.active_model_id is None

    payload = await supervisor.state_payload()
    assert payload.last_error is not None
    assert "500" in payload.last_error


async def test_a_failure_is_diagnosable_from_the_log_alone(
    supervisor: Supervisor, upstream: FakeUpstream, caplog: pytest.LogCaptureFixture
) -> None:
    """The M4 bar: /admin/logs must explain a failed activation without shell access.

    Whoever reads it is looking at a screenshot of the desktop app's log pane and
    cannot ssh anywhere, so the one ERROR line has to carry the model, the phase it
    died in, the backend's own words, and the fact that nothing will retry.
    """
    upstream.generate_status = 500
    with caplog.at_level("ERROR", logger="harness_control.supervisor.supervisor"):
        await activate_and_wait(supervisor, MODEL_ID)

    failures = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(failures) == 1
    message = failures[0].getMessage()

    assert MODEL_ID in message
    assert "loading weights" in message  # the phase, not just "failed"
    assert "500" in message  # what the backend actually said
    assert "ADR-0005" in message  # and that it will sit in error until asked again


async def test_a_model_that_never_becomes_ready_times_out(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """The daemon accepted the preload but the model never appeared in /api/ps."""
    supervisor._settings.load_timeout_s = 0.2  # private on purpose
    upstream.pin_on_generate = False

    await activate_and_wait(supervisor, MODEL_ID)

    assert supervisor.state is ModelState.ERROR


async def test_a_failure_does_not_crash_loop(
    supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """ADR-0005: a crashed model stays in error until a client activates something."""
    upstream.generate_status = 500
    await activate_and_wait(supervisor, MODEL_ID)
    await asyncio.sleep(0.15)

    assert supervisor.state is ModelState.ERROR
    assert len(upstream.generate_calls) == 1, "the supervisor retried on its own"


# --- L8: the watchdog ---------------------------------------------------------


async def test_l8_the_watchdog_notices_a_dead_backend(
    ready_supervisor: Supervisor, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L8 allows 5 s; the interval is shortened so the test does not take that long."""
    from harness_control.supervisor import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "WATCHDOG_INTERVAL_S", 0.05)
    ready_supervisor.start_watchdog()
    try:
        # The daemon dropped the model out from under us.
        upstream.loaded.clear()

        for _ in range(100):
            if ready_supervisor.state is ModelState.ERROR:
                break
            await asyncio.sleep(0.02)

        assert ready_supervisor.state is ModelState.ERROR
        payload = await ready_supervisor.state_payload()
        assert payload.last_error is not None
    finally:
        await ready_supervisor.stop_watchdog()


async def test_l8_the_watchdog_does_not_restart_anything(
    ready_supervisor: Supervisor, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0005 again, from the watchdog's side."""
    from harness_control.supervisor import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "WATCHDOG_INTERVAL_S", 0.05)
    ready_supervisor.start_watchdog()
    try:
        calls_before = len(upstream.generate_calls)
        upstream.loaded.clear()
        await asyncio.sleep(0.3)

        assert ready_supervisor.state is ModelState.ERROR
        assert len(upstream.generate_calls) == calls_before, "the watchdog tried to reload"
    finally:
        await ready_supervisor.stop_watchdog()


async def test_the_watchdog_leaves_a_switch_alone(
    ready_supervisor: Supervisor, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-switch the backend is legitimately unhealthy; that is not a death."""
    from harness_control.supervisor import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "WATCHDOG_INTERVAL_S", 0.02)
    ready_supervisor.start_watchdog()
    try:
        upstream.generate_delay_s = 0.2
        await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)
        assert ready_supervisor.state is ModelState.READY
    finally:
        await ready_supervisor.stop_watchdog()


# --- L11 and the state payload ------------------------------------------------


async def test_l11_gpu_is_empty_and_handled(ready_supervisor: Supervisor) -> None:
    payload = await ready_supervisor.state_payload()
    assert payload.gpu == []


async def test_the_state_payload_matches_the_contract(ready_supervisor: Supervisor) -> None:
    payload = await ready_supervisor.state_payload()

    assert payload.state is ModelState.READY
    assert payload.active_model_id == MODEL_ID
    assert payload.since.endswith("Z")
    assert payload.last_error is None


async def test_the_state_payload_reports_the_previous_model_after_a_switch(
    ready_supervisor: Supervisor,
) -> None:
    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)
    payload = await ready_supervisor.state_payload()

    assert payload.active_model_id == SECOND_MODEL_ID
    assert payload.previous_model_id == MODEL_ID


async def test_progress_hint_is_reported_while_loading(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    """The desktop app renders this verbatim under its progress bar."""
    upstream.generate_delay_s = 0.3
    await ready_supervisor.activate(SECOND_MODEL_ID)

    hints: list[str] = []

    async def sample() -> None:
        while True:
            payload = await ready_supervisor.state_payload()
            if payload.progress_hint:
                hints.append(payload.progress_hint)
            await asyncio.sleep(0.01)

    sampler = asyncio.create_task(sample())
    try:
        await settle(ready_supervisor)
    finally:
        sampler.cancel()

    assert hints, "nothing was reported during the switch"
    assert (await ready_supervisor.state_payload()).progress_hint is None


# --- restart restore ----------------------------------------------------------


async def test_the_active_model_is_persisted_for_a_restart(
    ready_supervisor: Supervisor,
) -> None:
    """SPEC §5.2: a reboot self-heals by restoring whatever was serving."""
    assert ready_supervisor.last_model() == MODEL_ID

    await activate_and_wait(ready_supervisor, SECOND_MODEL_ID)
    assert ready_supervisor.last_model() == SECOND_MODEL_ID


async def test_shutdown_unloads_and_is_safe_twice(
    ready_supervisor: Supervisor, upstream: FakeUpstream
) -> None:
    await ready_supervisor.shutdown()

    assert upstream.generate_calls[-1] == {"model": DEFAULT_MODEL, "keep_alive": 0}
    assert DEFAULT_MODEL not in upstream.loaded
    await ready_supervisor.shutdown()


# --- jobs ---------------------------------------------------------------------


async def test_job_ids_are_unique(ready_supervisor: Supervisor) -> None:
    ids = set()
    for model in (SECOND_MODEL_ID, MODEL_ID, SECOND_MODEL_ID):
        await activate_and_wait(ready_supervisor, model)
        ids.add(ready_supervisor.jobs.all()[-1].job_id)
    assert len(ids) == 3


def test_the_job_registry_is_bounded() -> None:
    """An unbounded registry is a slow memory leak on a long-running server."""
    from harness_control.supervisor.jobs import JobRegistry

    registry = JobRegistry(max_jobs=3)
    jobs = [registry.create("m") for _ in range(5)]
    assert len(registry) == 3
    assert registry.get(jobs[0].job_id) is None
    assert registry.get(jobs[-1].job_id) is not None


async def test_estimated_seconds_comes_from_the_catalog(supervisor: Supervisor) -> None:
    assert supervisor.estimated_seconds(MODEL_ID) == 10
    assert supervisor.estimated_seconds(SECOND_MODEL_ID) == 12
    assert supervisor.estimated_seconds("no-such-model") == 0
