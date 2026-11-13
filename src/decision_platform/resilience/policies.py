"""Retry with capped exponential backoff + full jitter, and a circuit breaker.

References:
* Marc Brooker, "Exponential Backoff And Jitter", AWS Architecture Blog (2015):
  full jitter, ``sleep = random(0, min(cap, base * 2**attempt))``.
* Amazon Builders' Library, "Timeouts, retries, and backoff with jitter":
  retry at a single layer, only retry idempotent operations, cap attempts.
* Michael Nygard, "Release It!" / Martin Fowler, "CircuitBreaker" bliki:
  closed -> open after N consecutive failures, half-open probe after a timeout.

Clock, sleep and randomness are injected so tests are fast and deterministic.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

T = TypeVar("T")


class DependencyError(Exception):
    """Base class for failures talking to an external dependency."""

    retryable = False


class TransientDependencyError(DependencyError):
    """Temporary failure: 5xx, connection reset, throttling. Safe to retry (calls are idempotent)."""

    retryable = True


class DependencyTimeoutError(TransientDependencyError):
    """The dependency did not answer within its timeout budget."""


class PermanentDependencyError(DependencyError):
    """Business or contract error (4xx, invalid subject). Retrying will not help."""

    retryable = False


class CircuitOpenError(TransientDependencyError):
    """Fail-fast: the breaker is open so the call was not attempted."""


@dataclass(frozen=True)
class BackoffPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.1
    max_delay_s: float = 2.0

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """Full-jitter delay before retry number ``attempt`` (0-based)."""
        cap = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        return (rng or random).uniform(0, cap)

    def ceiling(self, attempt: int) -> float:
        return min(self.max_delay_s, self.base_delay_s * (2**attempt))


@dataclass
class AttemptRecord:
    attempt: int
    ok: bool
    error_type: str | None = None
    error: str | None = None
    latency_ms: float = 0.0
    slept_s: float = 0.0


@dataclass
class RetryOutcome:
    attempts: list[AttemptRecord] = field(default_factory=list)


def call_with_retry(
    fn: Callable[[], T],
    policy: BackoffPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_attempt: Callable[[AttemptRecord], None] | None = None,
) -> tuple[T, RetryOutcome]:
    """Run ``fn`` retrying only retryable DependencyErrors. Re-raises the last error with ``.retry_outcome`` set."""
    outcome = RetryOutcome()
    for attempt in range(policy.max_attempts):
        started = clock()
        try:
            result = fn()
        except DependencyError as exc:
            rec = AttemptRecord(
                attempt=attempt + 1,
                ok=False,
                error_type=type(exc).__name__,
                error=str(exc),
                latency_ms=round((clock() - started) * 1000, 3),
            )
            last_attempt = attempt == policy.max_attempts - 1
            # Do not retry permanent errors, and do not hammer an open circuit.
            if not exc.retryable or isinstance(exc, CircuitOpenError) or last_attempt:
                outcome.attempts.append(rec)
                if on_attempt:
                    on_attempt(rec)
                exc.retry_outcome = outcome  # type: ignore[attr-defined]
                raise
            rec.slept_s = round(policy.delay(attempt, rng), 6)
            outcome.attempts.append(rec)
            if on_attempt:
                on_attempt(rec)
            sleep(rec.slept_s)
        else:
            rec = AttemptRecord(attempt=attempt + 1, ok=True, latency_ms=round((clock() - started) * 1000, 3))
            outcome.attempts.append(rec)
            if on_attempt:
                on_attempt(rec)
            return result, outcome
    raise AssertionError("unreachable")  # pragma: no cover


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Consecutive-failure circuit breaker. Thread-safe. Only transient errors count as failures."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self.transitions: list[tuple[str, str]] = []

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _set(self, new: BreakerState) -> None:
        if new != self._state:
            self.transitions.append((self._state.value, new.value))
            self._state = new

    def _maybe_half_open(self) -> None:
        if self._state == BreakerState.OPEN and self._clock() - self._opened_at >= self.reset_timeout_s:
            self._set(BreakerState.HALF_OPEN)

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            self._maybe_half_open()
            if self._state == BreakerState.OPEN:
                raise CircuitOpenError(f"circuit '{self.name}' is open; failing fast")
        try:
            result = fn()
        except TransientDependencyError:
            with self._lock:
                self._failures += 1
                if self._state == BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
                    self._set(BreakerState.OPEN)
                    self._opened_at = self._clock()
            raise
        else:
            with self._lock:
                self._failures = 0
                self._set(BreakerState.CLOSED)
            return result

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._set(BreakerState.CLOSED)
