"""Unit tests for backoff, retry helper, circuit breaker, and the transactional outbox relay."""

import random

import pytest

from decision_platform.resilience.policies import (
    BackoffPolicy,
    CircuitBreaker,
    CircuitOpenError,
    PermanentDependencyError,
    TransientDependencyError,
    call_with_retry,
)


def test_full_jitter_bounds():
    p = BackoffPolicy(max_attempts=6, base_delay_s=0.1, max_delay_s=1.0)
    rng = random.Random(0)
    for attempt in range(8):
        ceiling = min(1.0, 0.1 * 2**attempt)
        samples = [p.delay(attempt, rng) for _ in range(200)]
        assert all(0 <= s <= ceiling for s in samples)
        assert max(samples) > ceiling * 0.8  # actually spreads across the window


def test_call_with_retry_stops_on_permanent():
    calls = []

    def fn():
        calls.append(1)
        raise PermanentDependencyError("400")

    with pytest.raises(PermanentDependencyError) as exc:
        call_with_retry(fn, BackoffPolicy(5), sleep=lambda s: None)
    assert len(calls) == 1 and len(exc.value.retry_outcome.attempts) == 1


def test_call_with_retry_eventually_succeeds():
    seq = iter([TransientDependencyError("a"), TransientDependencyError("b"), "ok"])

    def fn():
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    slept = []
    result, outcome = call_with_retry(fn, BackoffPolicy(3, 0.1, 1), sleep=slept.append, rng=random.Random(1))
    assert result == "ok" and [a.ok for a in outcome.attempts] == [False, False, True] and len(slept) == 2


def test_breaker_lifecycle():
    t = [0.0]
    cb = CircuitBreaker("x", failure_threshold=2, reset_timeout_s=10, clock=lambda: t[0])

    def bad():
        raise TransientDependencyError("down")

    for _ in range(2):
        with pytest.raises(TransientDependencyError):
            cb.call(bad)
    assert cb.state.value == "open"
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "never called")
    t[0] = 10
    assert cb.state.value == "half_open"
    with pytest.raises(TransientDependencyError):
        cb.call(bad)  # probe fails -> open again
    assert cb.state.value == "open"
    t[0] = 25
    assert cb.call(lambda: "ok") == "ok" and cb.state.value == "closed"
    assert cb.transitions == [
        ("closed", "open"),
        ("open", "half_open"),
        ("half_open", "open"),
        ("open", "half_open"),
        ("half_open", "closed"),
    ]


def test_outbox_relay_is_at_least_once_and_retries(svc, loan):
    svc.submit("loan_application", loan(), "ob-1")
    svc.submit("loan_application", loan(applicant={"id": "APP-1003"}), "ob-2")
    notifier = svc.hub.get("notifier")
    notifier.configure(fail_next=["transient"])
    first = svc.relay_outbox()
    assert first == {"published": 1, "failed": 1, "pending": 1}
    second = svc.relay_outbox()
    assert second == {"published": 1, "failed": 0, "pending": 0}
    row = svc.db.read().execute("SELECT attempts, last_error FROM outbox ORDER BY id LIMIT 1").fetchone()
    assert row["attempts"] == 2 and row["last_error"] is None
    assert svc.relay_outbox() == {"published": 0, "failed": 0, "pending": 0}
