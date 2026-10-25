from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from compliance.observability import metrics


class CircuitOpenError(RuntimeError):
    pass


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    threshold: int = 5
    cooldown_s: float = 30.0
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    half_open_probe: bool = False
    transitions: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        self._publish()

    def _publish(self) -> None:
        for state in CircuitState:
            metrics.CIRCUIT_STATE.labels(state=state.value).set(1.0 if self.state == state else 0.0)

    def _transition(self, state: CircuitState) -> None:
        if self.state == state:
            return
        self.state = state
        self.transitions += 1
        metrics.CIRCUIT_TRANSITIONS.labels(to_state=state.value).inc()
        self._publish()

    def before_call(self) -> None:
        now = time.monotonic()
        if self.state == CircuitState.OPEN:
            if now - self.opened_at >= self.cooldown_s:
                self._transition(CircuitState.HALF_OPEN)
                self.half_open_probe = True
                return
            raise CircuitOpenError(
                f"circuit open after {self.failures} failures; retry in "
                f"{max(0.0, self.cooldown_s - (now - self.opened_at)):.1f}s"
            )
        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_probe:
                raise CircuitOpenError("circuit half-open; one probe already in flight")
            self.half_open_probe = True

    def record_success(self) -> None:
        self.failures = 0
        self.half_open_probe = False
        self._transition(CircuitState.CLOSED)

    def record_failure(self, error: str) -> None:
        self.failures += 1
        self.last_error = error
        self.half_open_probe = False
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            self._transition(CircuitState.OPEN)
        elif self.state == CircuitState.HALF_OPEN:
            self.opened_at = time.monotonic()
            self._transition(CircuitState.OPEN)

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "failures": self.failures,
            "threshold": self.threshold,
            "cooldown_s": self.cooldown_s,
            "seconds_until_probe": (
                round(max(0.0, self.cooldown_s - (time.monotonic() - self.opened_at)), 1)
                if self.state == CircuitState.OPEN
                else 0.0
            ),
            "last_error": self.last_error,
            "transitions": self.transitions,
        }
