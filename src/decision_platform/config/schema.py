"""Declarative workflow configuration model.

A workflow is a versioned YAML/JSON document. Everything business-specific
(input shape, thresholds, rules, routing, dependencies, retry budgets) lives
here, so changing a rule or a whole workflow is a configuration change, not a
code change.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TERMINALS = frozenset({"approve", "reject", "manual_review"})
RESERVED_NAMESPACES = frozenset({"input", "params", "derived", "meta"})
_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleAction(str, Enum):
    """What a failed rule does to the stage outcome. Ordered by severity."""

    FLAG = "flag"  # recorded, non-blocking
    REVIEW = "review"  # route to manual review
    REJECT = "reject"  # hard reject


SEVERITY = {RuleAction.FLAG: 1, RuleAction.REVIEW: 2, RuleAction.REJECT: 3}


class RuleKind(str, Enum):
    MANDATORY = "mandatory"  # must hold for every request
    THRESHOLD = "threshold"  # numeric limit, usually parameterised
    CONDITIONAL = "conditional"  # only applies when `when` is true


class RetryPolicyConfig(_Strict):
    max_attempts: int = Field(3, ge=1, le=10)
    base_delay_ms: int = Field(100, ge=0, le=60_000)
    max_delay_ms: int = Field(2_000, ge=0, le=300_000)


class CircuitBreakerConfig(_Strict):
    failure_threshold: int = Field(5, ge=1, le=1000)
    reset_timeout_s: float = Field(30.0, gt=0, le=3600)


class DependencyConfig(_Strict):
    type: str = Field(..., description="Registered dependency adapter type")
    timeout_ms: int = Field(1_000, ge=1, le=60_000)
    retry: RetryPolicyConfig = RetryPolicyConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()


class RuleConfig(_Strict):
    id: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")
    name: str
    kind: RuleKind = RuleKind.MANDATORY
    description: str = ""
    when: str | None = Field(None, description="Precondition; rule is SKIPPED when false")
    condition: str = Field(..., description="Must evaluate truthy for the rule to PASS")
    on_fail: RuleAction = RuleAction.REJECT
    on_missing: RuleAction | None = Field(
        None, description="Action when referenced data is absent. Defaults to on_fail for mandatory rules, else review."
    )
    reason: str = Field(..., description="Human-readable reason template, e.g. 'Score {credit.score} < {params.min}'")

    @model_validator(mode="after")
    def _conditional_needs_when(self) -> RuleConfig:
        if self.kind == RuleKind.CONDITIONAL and not self.when:
            raise ValueError(f"rule {self.id}: conditional rules require a 'when' clause")
        return self

    @property
    def effective_on_missing(self) -> RuleAction:
        if self.on_missing is not None:
            return self.on_missing
        return self.on_fail if self.kind == RuleKind.MANDATORY else RuleAction.REVIEW


class _StageBase(_Strict):
    id: str = Field(..., pattern=_ID_PATTERN)
    description: str = ""

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in TERMINALS:
            raise ValueError(f"stage id '{v}' is reserved for terminal outcomes")
        return v


class RulesStage(_StageBase):
    type: Literal["rules"]
    evaluation: Literal["all", "first_failure"] = "all"
    rules: list[RuleConfig] = Field(..., min_length=1)
    next: str
    on_review: str = "manual_review"
    on_reject: str = "reject"

    def targets(self) -> dict[str, str]:
        return {"pass": self.next, "review": self.on_review, "reject": self.on_reject}


class ComputeStage(_StageBase):
    type: Literal["compute"]
    outputs: dict[str, str] = Field(..., min_length=1, description="name -> expression; stored under derived.<name>")
    next: str

    @field_validator("outputs")
    @classmethod
    def _valid_names(cls, v: dict[str, str]) -> dict[str, str]:
        import re

        for name in v:
            if not re.match(_ID_PATTERN, name):
                raise ValueError(f"invalid derived value name '{name}'")
        return v

    def targets(self) -> dict[str, str]:
        return {"pass": self.next}


class DependencyStage(_StageBase):
    type: Literal["dependency"]
    dependency: str
    request: dict[str, str] = Field(default_factory=dict, description="field -> expression sent to the dependency")
    output_key: str = Field(..., pattern=_ID_PATTERN)
    next: str
    on_unavailable: Literal["retry_later", "manual_review", "reject"] = "retry_later"
    on_error: Literal["manual_review", "reject"] = "manual_review"

    @field_validator("output_key")
    @classmethod
    def _not_reserved_ns(cls, v: str) -> str:
        if v in RESERVED_NAMESPACES:
            raise ValueError(f"output_key '{v}' collides with a reserved namespace")
        return v

    def targets(self) -> dict[str, str]:
        t = {"pass": self.next, "error": self.on_error}
        if self.on_unavailable != "retry_later":
            t["unavailable"] = self.on_unavailable
        return t


class Branch(_Strict):
    when: str
    next: str
    label: str = ""


class BranchStage(_StageBase):
    type: Literal["branch"]
    branches: list[Branch] = Field(..., min_length=1)
    default: str

    def targets(self) -> dict[str, str]:
        t = {f"branch[{i}]": b.next for i, b in enumerate(self.branches)}
        t["default"] = self.default
        return t


Stage = Annotated[RulesStage | ComputeStage | DependencyStage | BranchStage, Field(discriminator="type")]


class DuplicateCheckConfig(_Strict):
    """Business-level duplicate detection (distinct from transport idempotency keys)."""

    fields: list[str] = Field(..., min_length=1, description="input paths forming the business key")
    window_hours: int = Field(24, ge=1, le=24 * 365)
    action: Literal["return_existing", "reject"] = "return_existing"


class RequestRetryConfig(_Strict):
    """Retry budget for a request parked in RETRY_PENDING after inline retries are exhausted."""

    max_request_retries: int = Field(3, ge=0, le=50)
    base_delay_s: float = Field(30.0, ge=0)
    max_delay_s: float = Field(900.0, ge=0)
    on_exhausted: Literal["failed", "manual_review"] = "manual_review"


class WorkflowConfig(_Strict):
    workflow: str = Field(..., pattern=_ID_PATTERN)
    version: int = Field(..., ge=1)
    description: str = ""
    owner: str = ""
    input_schema: dict[str, Any]
    parameters: dict[str, Any] = Field(default_factory=dict)
    dependencies: dict[str, DependencyConfig] = Field(default_factory=dict)
    duplicate_check: DuplicateCheckConfig | None = None
    retry: RequestRetryConfig = RequestRetryConfig()
    start: str | None = None
    stages: list[Stage] = Field(..., min_length=1)

    @property
    def start_stage(self) -> str:
        return self.start or self.stages[0].id

    def stage_map(self) -> dict[str, Any]:
        return {s.id: s for s in self.stages}
