"""Core domain types: request lifecycle states and the state machine guarding them."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RequestStatus(str, Enum):
    RECEIVED = "RECEIVED"  # validated and persisted, not yet processed
    IN_PROGRESS = "IN_PROGRESS"  # engine is executing stages
    RETRY_PENDING = "RETRY_PENDING"  # parked after a dependency outage; resumes from checkpoint
    MANUAL_REVIEW = "MANUAL_REVIEW"  # waiting for a human decision
    APPROVED = "APPROVED"  # terminal
    REJECTED = "REJECTED"  # terminal
    FAILED = "FAILED"  # terminal: unrecoverable technical failure

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({RequestStatus.APPROVED, RequestStatus.REJECTED, RequestStatus.FAILED})

# Explicit allow-list. Anything not listed is a bug and raises IllegalTransition.
ALLOWED_TRANSITIONS: dict[RequestStatus, frozenset[RequestStatus]] = {
    RequestStatus.RECEIVED: frozenset({RequestStatus.IN_PROGRESS}),
    RequestStatus.IN_PROGRESS: frozenset(
        {
            RequestStatus.APPROVED,
            RequestStatus.REJECTED,
            RequestStatus.MANUAL_REVIEW,
            RequestStatus.RETRY_PENDING,
            RequestStatus.FAILED,
        }
    ),
    RequestStatus.RETRY_PENDING: frozenset(
        {RequestStatus.IN_PROGRESS, RequestStatus.FAILED, RequestStatus.MANUAL_REVIEW}
    ),
    RequestStatus.MANUAL_REVIEW: frozenset({RequestStatus.APPROVED, RequestStatus.REJECTED}),
    RequestStatus.APPROVED: frozenset(),
    RequestStatus.REJECTED: frozenset(),
    RequestStatus.FAILED: frozenset(),
}

TERMINAL_TARGET_TO_STATUS = {
    "approve": RequestStatus.APPROVED,
    "reject": RequestStatus.REJECTED,
    "manual_review": RequestStatus.MANUAL_REVIEW,
}


class IllegalTransition(RuntimeError):
    def __init__(self, src: RequestStatus, dst: RequestStatus):
        super().__init__(f"illegal state transition {src.value} -> {dst.value}")
        self.src, self.dst = src, dst


def check_transition(src: RequestStatus, dst: RequestStatus) -> None:
    if dst not in ALLOWED_TRANSITIONS[src]:
        raise IllegalTransition(src, dst)


class RuleOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"  # conditional rule whose precondition was false
    MISSING_DATA = "MISSING_DATA"
    ERROR = "ERROR"  # evaluation error (e.g. type mismatch); treated like missing data


@dataclass
class RuleTrace:
    rule_id: str
    name: str
    kind: str
    outcome: RuleOutcome
    action: str | None  # flag / review / reject applied, None when passed or skipped
    condition: str
    when: str | None
    data_refs: dict[str, Any]  # every variable read with its value: the "data reference"
    reason: str | None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "kind": self.kind,
            "outcome": self.outcome.value,
            "action": self.action,
            "condition": self.condition,
            "when": self.when,
            "data_refs": self.data_refs,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class StageResult:
    """What a stage handler returns to the engine. Handlers never touch persistence."""

    outcome: str  # pass | review | reject | error | unavailable | branch label
    next_target: str | None  # stage id, terminal name, or None for RETRY_PENDING
    context_updates: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
