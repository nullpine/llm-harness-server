"""The externally visible state machine (`docs/API-CONTRACT.md` §3).

Pure: no I/O, no async, no backend imports. Everything here is a value or a
predicate over values, which is what makes `tests/test_state_machine.py` able to
cover the whole space exhaustively.

    idle ──activate──> loading ──ready──> ready
      ▲                   │                 │
      │                   └──fail──> error  │
      │                                │    │
      └────────── stopping <───────────┴────┘
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class ModelState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    READY = "ready"
    STOPPING = "stopping"
    ERROR = "error"


#: The only transitions the supervisor may make. A backend never sets state
#: (`.claude/rules/backend-boundary.md`); it reports facts and the supervisor
#: decides which of these edges to walk.
_TRANSITIONS: Mapping[ModelState, frozenset[ModelState]] = MappingProxyType(
    {
        # An activation from rest goes straight through the drain, which is a
        # no-op when nothing is loaded — so idle reaches both stopping and loading.
        ModelState.IDLE: frozenset({ModelState.STOPPING, ModelState.LOADING}),
        ModelState.LOADING: frozenset({ModelState.READY, ModelState.ERROR, ModelState.STOPPING}),
        # ready → error is the watchdog noticing the backend died under us.
        ModelState.READY: frozenset({ModelState.STOPPING, ModelState.ERROR}),
        ModelState.STOPPING: frozenset({ModelState.LOADING, ModelState.IDLE, ModelState.ERROR}),
        # A failed model stays in error until a client activates something.
        # No crash-loop restarts — ADR-0005.
        ModelState.ERROR: frozenset({ModelState.STOPPING, ModelState.LOADING, ModelState.IDLE}),
    }
)

#: States in which `/v1/chat/completions` must not be proxied, and what the
#: contract says to answer instead (contract §3, state table).
SERVING_STATES: frozenset[ModelState] = frozenset({ModelState.READY})
LOADING_STATES: frozenset[ModelState] = frozenset({ModelState.LOADING, ModelState.STOPPING})
NOT_ACTIVE_STATES: frozenset[ModelState] = frozenset({ModelState.IDLE, ModelState.ERROR})


class IllegalTransitionError(Exception):
    """Raised when the supervisor is asked to make a transition the machine forbids.

    This is a programming error, not a client error: it never becomes an HTTP
    response, it fails the test that provoked it.
    """

    def __init__(self, source: ModelState, target: ModelState) -> None:
        super().__init__(f"illegal state transition: {source.value} -> {target.value}")
        self.source = source
        self.target = target


def allowed_targets(source: ModelState) -> frozenset[ModelState]:
    """Every state reachable in one step from `source`."""
    return _TRANSITIONS[source]


def can_transition(source: ModelState, target: ModelState) -> bool:
    """True when `source -> target` is a legal edge. Self-edges are not edges."""
    return target in _TRANSITIONS[source]


def check_transition(source: ModelState, target: ModelState) -> None:
    """Raise `IllegalTransitionError` unless `source -> target` is legal."""
    if not can_transition(source, target):
        raise IllegalTransitionError(source, target)


def can_serve(state: ModelState) -> bool:
    """True when `/v1/chat/completions` may be proxied upstream."""
    return state in SERVING_STATES
