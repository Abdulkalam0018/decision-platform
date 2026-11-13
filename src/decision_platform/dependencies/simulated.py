"""Simulated external dependencies with controllable fault injection.

Each adapter mimics a real third-party service (credit bureau, fraud screen,
document verifier...). They are deterministic: the same input always yields
the same output, which keeps tests and decision examples reproducible.

Two behaviours are modelled explicitly:

* **Faults**: a ``FaultPlan`` can force the next N calls to fail with a
  transient error, a timeout or a permanent error, apply a random failure rate,
  mark the service as fully down, or add latency beyond the caller's timeout.
* **Idempotency**: every call carries an idempotency token
  (``<request_id>:<stage_id>``). A repeated token returns the stored result
  instead of re-executing the side effect, exactly like Stripe-style keys or
  AWS ``ClientToken``. ``side_effects`` counts real executions so tests can
  prove a retry never double-charges / double-pulls a credit report.
"""

from __future__ import annotations

import copy
import hashlib
import random
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..resilience.policies import (
    CircuitBreaker,
    DependencyTimeoutError,
    PermanentDependencyError,
    TransientDependencyError,
)


def _stable_int(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


@dataclass
class FaultPlan:
    fail_next: deque[str] = field(default_factory=deque)  # "transient" | "timeout" | "permanent"
    failure_rate: float = 0.0
    down: bool = False
    latency_ms: int = 15
    seed: int = 7
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def next_fault(self) -> str | None:
        if self.down:
            return "transient"
        if self.fail_next:
            return self.fail_next.popleft()
        if self.failure_rate and self._rng.random() < self.failure_rate:
            return "transient"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fail_next": list(self.fail_next),
            "failure_rate": self.failure_rate,
            "down": self.down,
            "latency_ms": self.latency_ms,
        }


class SimulatedDependency:
    def __init__(self, type_name: str, responder: Callable[[dict[str, Any]], dict[str, Any]], latency_ms: int = 15):
        self.type = type_name
        self._responder = responder
        self.faults = FaultPlan(latency_ms=latency_ms)
        self.calls = 0
        self.side_effects = 0
        self._results: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def configure(
        self,
        *,
        fail_next: list[str] | None = None,
        failure_rate: float | None = None,
        down: bool | None = None,
        latency_ms: int | None = None,
    ) -> None:
        with self._lock:
            if fail_next is not None:
                for kind in fail_next:
                    if kind not in ("transient", "timeout", "permanent"):
                        raise ValueError(f"unknown fault kind '{kind}'")
                self.faults.fail_next = deque(fail_next)
            if failure_rate is not None:
                if not 0 <= failure_rate <= 1:
                    raise ValueError("failure_rate must be within [0, 1]")
                self.faults.failure_rate = failure_rate
            if down is not None:
                self.faults.down = down
            if latency_ms is not None:
                self.faults.latency_ms = latency_ms

    def reset(self) -> None:
        with self._lock:
            self.faults = FaultPlan(latency_ms=15)
            self.calls = 0
            self.side_effects = 0
            self._results.clear()

    def invoke(self, payload: dict[str, Any], idempotency_token: str, timeout_ms: int) -> dict[str, Any]:
        with self._lock:
            self.calls += 1
            fault = self.faults.next_fault()
            latency = self.faults.latency_ms
        if fault == "timeout" or latency > timeout_ms:
            raise DependencyTimeoutError(f"{self.type}: no response within {timeout_ms} ms")
        if fault == "transient":
            raise TransientDependencyError(f"{self.type}: 503 Service Unavailable")
        if fault == "permanent":
            raise PermanentDependencyError(f"{self.type}: 400 Bad Request (rejected payload)")
        with self._lock:
            if idempotency_token in self._results:
                return copy.deepcopy(self._results[idempotency_token])
        result = self._responder(payload)  # may raise PermanentDependencyError
        with self._lock:
            self._results[idempotency_token] = copy.deepcopy(result)
            self.side_effects += 1
        return result


# Responders
# Fixture subjects give predictable outcomes for demos and tests. Anything else
# gets a deterministic pseudo-random but stable answer derived from a hash.

CREDIT_FIXTURES = {
    "APP-1001": {"score": 782, "open_defaults": 0},
    "APP-1002": {"score": 541, "open_defaults": 2},
    "APP-1003": {"score": 668, "open_defaults": 0},
    "APP-1004": {"score": 705, "open_defaults": 1},
    "APP-1005": {"score": 634, "open_defaults": 0},
}


def credit_bureau(payload: dict[str, Any]) -> dict[str, Any]:
    subject = str(payload.get("applicant_id", ""))
    if not subject or subject.startswith("UNKNOWN"):
        raise PermanentDependencyError("credit_bureau: 404 subject not found")
    fx = CREDIT_FIXTURES.get(subject)
    score = fx["score"] if fx else 300 + _stable_int("credit", subject) % 551
    defaults = fx["open_defaults"] if fx else _stable_int("defaults", subject) % 3
    return {
        "report_id": f"CR-{_stable_int('report', subject) % 10**8:08d}",
        "score": score,
        "open_defaults": defaults,
        "bureau": "SimBureau",
    }


def fraud_screen(payload: dict[str, Any]) -> dict[str, Any]:
    email = str(payload.get("email", ""))
    risk = 5 if email.endswith("@example.com") else _stable_int("fraud", email) % 100
    if "fraud" in email:
        risk = 97
    return {"risk_score": risk, "signals": ["disposable_email"] if risk > 90 else []}


def document_verifier(payload: dict[str, Any]) -> dict[str, Any]:
    docs = payload.get("documents") or []
    results = []
    for d in docs:
        number = str(d.get("number", ""))
        ok = bool(number) and "FAKE" not in number.upper()
        results.append({"type": d.get("type"), "verified": ok, "confidence": 0.98 if ok else 0.12})
    return {
        "all_verified": bool(results) and all(r["verified"] for r in results),
        "results": results,
        "min_confidence": min((r["confidence"] for r in results), default=0.0),
    }


POLICY_FIXTURES = {
    "POL-500": {"active": True, "coverage_limit": 500000, "deductible": 5000, "policy_start": "2024-01-15"},
    "POL-777": {"active": True, "coverage_limit": 200000, "deductible": 10000, "policy_start": "2026-10-01"},
    "POL-404": {"active": False, "coverage_limit": 0, "deductible": 0, "policy_start": "2019-06-01"},
}


def policy_lookup(payload: dict[str, Any]) -> dict[str, Any]:
    number = str(payload.get("policy_number", ""))
    if number not in POLICY_FIXTURES:
        raise PermanentDependencyError(f"policy_lookup: 404 policy '{number}' not found")
    return {"policy_number": number, **POLICY_FIXTURES[number]}


def background_check(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("candidate_id", ""))
    flagged = cid.endswith("-X")
    return {"status": "consider" if flagged else "clear", "checks": ["identity", "employment", "criminal"]}


SANCTIONED_COUNTRIES = {"XX", "YY"}


def sanctions_screen(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("vendor_name", "")).upper()
    country = str(payload.get("country", "")).upper()
    hit = "SANCTIONED" in name or country in SANCTIONED_COUNTRIES
    return {"match": hit, "lists_checked": ["OFAC-SDN", "EU-CFSP", "UN-SC"], "match_score": 0.99 if hit else 0.0}


def notifier(payload: dict[str, Any]) -> dict[str, Any]:
    return {"delivered": True, "channel": payload.get("channel", "webhook")}


DEFAULT_RESPONDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "credit_bureau": credit_bureau,
    "fraud_screen": fraud_screen,
    "document_verifier": document_verifier,
    "policy_lookup": policy_lookup,
    "background_check": background_check,
    "sanctions_screen": sanctions_screen,
    "notifier": notifier,
}


class DependencyHub:
    """Registry of adapters plus circuit breakers.

    Breakers are keyed by ``<workflow>:<dependency name>`` so each workflow's configured thresholds are
    honoured exactly (a new workflow version with different thresholds updates its breaker). The trade-off:
    two workflows calling the same service each learn about an outage separately (see TRADEOFFS ADR-10).
    """

    def __init__(self, clock: Callable[[], float] | None = None):
        import time

        self._clock = clock or time.monotonic
        self.adapters: dict[str, SimulatedDependency] = {
            name: SimulatedDependency(name, fn) for name, fn in DEFAULT_RESPONDERS.items()
        }
        self.breakers: dict[str, CircuitBreaker] = {}
        self.breaker_types: dict[str, str] = {}  # breaker key -> adapter type
        self._lock = threading.Lock()

    @property
    def types(self) -> list[str]:
        return sorted(self.adapters)

    def register(self, type_name: str, responder: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.adapters[type_name] = SimulatedDependency(type_name, responder)

    def get(self, type_name: str) -> SimulatedDependency:
        if type_name not in self.adapters:
            raise KeyError(f"no dependency adapter registered for type '{type_name}'")
        return self.adapters[type_name]

    def breaker(
        self, key: str, failure_threshold: int, reset_timeout_s: float, type_name: str | None = None
    ) -> CircuitBreaker:
        with self._lock:
            if type_name:
                self.breaker_types[key] = type_name
            b = self.breakers.get(key)
            if b is None:
                b = self.breakers[key] = CircuitBreaker(key, failure_threshold, reset_timeout_s, self._clock)
            else:  # configuration may change with a new workflow version
                b.failure_threshold, b.reset_timeout_s = failure_threshold, reset_timeout_s
            return b

    def reset(self) -> None:
        for a in self.adapters.values():
            a.reset()
        for b in self.breakers.values():
            b.reset()

    def status(self) -> dict[str, Any]:
        return {
            name: {
                "faults": a.faults.to_dict(),
                "calls": a.calls,
                "side_effects": a.side_effects,
                "circuits": {k: b.state.value for k, b in self.breakers.items() if self.breaker_types.get(k) == name},
            }
            for name, a in self.adapters.items()
        }
