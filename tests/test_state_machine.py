"""Every legal transition is allowed and every illegal one raises.

The machine is small enough to test exhaustively, so this does: it walks the full
`ModelState x ModelState` product rather than a hand-picked sample. A new state
added without a transition table entry fails here, which is the point.
"""

import itertools

import pytest

from harness_control.supervisor.state import (
    LOADING_STATES,
    NOT_ACTIVE_STATES,
    SERVING_STATES,
    IllegalTransitionError,
    ModelState,
    allowed_targets,
    can_serve,
    can_transition,
    check_transition,
)

#: The transitions the contract's diagram permits, written out by hand so that a
#: typo in the implementation's table does not silently become the expectation.
LEGAL: set[tuple[ModelState, ModelState]] = {
    (ModelState.IDLE, ModelState.STOPPING),
    (ModelState.IDLE, ModelState.LOADING),
    (ModelState.LOADING, ModelState.READY),
    (ModelState.LOADING, ModelState.ERROR),
    (ModelState.LOADING, ModelState.STOPPING),
    (ModelState.READY, ModelState.STOPPING),
    (ModelState.READY, ModelState.ERROR),
    (ModelState.STOPPING, ModelState.LOADING),
    (ModelState.STOPPING, ModelState.IDLE),
    (ModelState.STOPPING, ModelState.ERROR),
    (ModelState.ERROR, ModelState.STOPPING),
    (ModelState.ERROR, ModelState.LOADING),
    (ModelState.ERROR, ModelState.IDLE),
}

ALL_PAIRS = list(itertools.product(ModelState, ModelState))
ILLEGAL = [pair for pair in ALL_PAIRS if pair not in LEGAL]


def test_the_state_values_are_exactly_the_contracts() -> None:
    assert {s.value for s in ModelState} == {"idle", "loading", "ready", "stopping", "error"}


@pytest.mark.parametrize(("source", "target"), sorted(LEGAL))
def test_legal_transitions_are_permitted(source: ModelState, target: ModelState) -> None:
    assert can_transition(source, target)
    check_transition(source, target)  # must not raise
    assert target in allowed_targets(source)


@pytest.mark.parametrize(("source", "target"), ILLEGAL)
def test_illegal_transitions_raise(source: ModelState, target: ModelState) -> None:
    assert not can_transition(source, target)
    with pytest.raises(IllegalTransitionError) as excinfo:
        check_transition(source, target)
    assert excinfo.value.source is source
    assert excinfo.value.target is target
    assert source.value in str(excinfo.value)
    assert target.value in str(excinfo.value)


@pytest.mark.parametrize("state", list(ModelState))
def test_self_transitions_are_not_edges(state: ModelState) -> None:
    """Staying put is not a transition; the supervisor must not "re-enter" a state."""
    assert not can_transition(state, state)


@pytest.mark.parametrize("state", list(ModelState))
def test_every_state_has_a_transition_entry(state: ModelState) -> None:
    assert isinstance(allowed_targets(state), frozenset)


def test_only_ready_may_serve() -> None:
    assert {ModelState.READY} == SERVING_STATES
    for state in ModelState:
        assert can_serve(state) is (state is ModelState.READY)


def test_the_three_serving_buckets_partition_the_states() -> None:
    """Every state maps to exactly one behaviour on /v1/chat/completions."""
    buckets = [SERVING_STATES, LOADING_STATES, NOT_ACTIVE_STATES]
    union: set[ModelState] = set()
    for bucket in buckets:
        assert not (union & bucket), "a state falls into two behaviours"
        union |= bucket
    assert union == set(ModelState)


def test_no_state_is_a_dead_end() -> None:
    """Every state can be left, or a model could get permanently stuck."""
    for state in ModelState:
        assert allowed_targets(state), f"{state} is a dead end"
