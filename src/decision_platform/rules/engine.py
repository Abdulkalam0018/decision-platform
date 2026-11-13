"""Rule-set evaluation with full trace.

Hit policy: evaluate every applicable rule (``evaluation: all``) and take the
most severe action (reject > review > flag/pass). This is DMN's "Collect"
hit policy with a MAX-severity aggregator, chosen because an explanation must
list *every* reason a request failed, not only the first one. A stage can opt
into ``first_failure`` for cost reasons, which behaves like DMN "First".
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..config.compiler import CompiledWorkflow
from ..config.schema import SEVERITY, RuleAction, RulesStage
from ..domain.models import RuleOutcome, RuleTrace
from .expression import EvalContext, MissingDataError, render_template

# Any exception raised while evaluating user-authored configuration is contained and traced as ERROR;
# it must never crash the engine or leave a request stuck.
EVAL_ERRORS: tuple[type[BaseException], ...] = (Exception,)


def _snapshot(reads: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in sorted(reads.items())}


def evaluate_rules(
    stage: RulesStage, wf: CompiledWorkflow, variables: dict[str, Any], today: date
) -> tuple[str, list[RuleTrace]]:
    traces: list[RuleTrace] = []
    worst: RuleAction | None = None

    for rule in stage.rules:
        ctx = EvalContext(variables, today)
        base: dict[str, Any] = dict(
            rule_id=rule.id, name=rule.name, kind=rule.kind.value, condition=rule.condition, when=rule.when
        )

        if rule.when:
            try:
                applies = bool(wf.expr(rule.when).evaluate(ctx))
            except MissingDataError as exc:
                action = rule.effective_on_missing
                traces.append(
                    RuleTrace(
                        **base,
                        outcome=RuleOutcome.MISSING_DATA,
                        action=action.value,
                        data_refs=_snapshot(ctx.reads),
                        reason=f"{rule.name}: required data '{exc.path}' is missing",
                        detail="precondition could not be evaluated",
                    )
                )
                worst = _max(worst, action)
                if _stop(stage, action):
                    break
                continue
            except EVAL_ERRORS as exc:
                action = rule.effective_on_missing
                traces.append(
                    RuleTrace(
                        **base,
                        outcome=RuleOutcome.ERROR,
                        action=action.value,
                        data_refs=_snapshot(ctx.reads),
                        reason=f"{rule.name}: precondition could not be evaluated",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                worst = _max(worst, action)
                if _stop(stage, action):
                    break
                continue
            if not applies:
                traces.append(
                    RuleTrace(
                        **base,
                        outcome=RuleOutcome.SKIPPED,
                        action=None,
                        data_refs=_snapshot(ctx.reads),
                        reason=None,
                        detail="precondition false; rule not applicable",
                    )
                )
                continue

        try:
            passed = bool(wf.expr(rule.condition).evaluate(ctx))
        except MissingDataError as exc:
            action = rule.effective_on_missing
            traces.append(
                RuleTrace(
                    **base,
                    outcome=RuleOutcome.MISSING_DATA,
                    action=action.value,
                    data_refs=_snapshot(ctx.reads),
                    reason=f"{rule.name}: required data '{exc.path}' is missing",
                )
            )
            worst = _max(worst, action)
            if _stop(stage, action):
                break
            continue
        except EVAL_ERRORS as exc:
            action = rule.effective_on_missing
            traces.append(
                RuleTrace(
                    **base,
                    outcome=RuleOutcome.ERROR,
                    action=action.value,
                    data_refs=_snapshot(ctx.reads),
                    reason=f"{rule.name}: could not be evaluated",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            worst = _max(worst, action)
            if _stop(stage, action):
                break
            continue

        if passed:
            traces.append(
                RuleTrace(**base, outcome=RuleOutcome.PASS, action=None, data_refs=_snapshot(ctx.reads), reason=None)
            )
            continue

        reason = render_template(rule.reason, EvalContext(variables, today))
        traces.append(
            RuleTrace(
                **base,
                outcome=RuleOutcome.FAIL,
                action=rule.on_fail.value,
                data_refs=_snapshot(ctx.reads),
                reason=reason,
            )
        )
        worst = _max(worst, rule.on_fail)
        if _stop(stage, rule.on_fail):
            break

    if worst == RuleAction.REJECT:
        return "reject", traces
    if worst == RuleAction.REVIEW:
        return "review", traces
    return "pass", traces


def _max(a: RuleAction | None, b: RuleAction) -> RuleAction:
    if a is None or SEVERITY[b] > SEVERITY[a]:
        return b
    return a


def _stop(stage: RulesStage, action: RuleAction) -> bool:
    return stage.evaluation == "first_failure" and action in (RuleAction.REJECT, RuleAction.REVIEW)
