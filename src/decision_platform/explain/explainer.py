"""Turn a request's stored trace into a structured and a plain-language explanation.

Everything here is derived from persisted data (stage runs, transitions,
reviews, audit head), never recomputed, so the explanation is exactly what
the engine did at the time, under the rule version that was pinned.
"""

from __future__ import annotations

from typing import Any

from ..persistence.repository import RequestRow


def _fmt_refs(refs: dict[str, Any]) -> str:
    if not refs:
        return "no data read"
    return ", ".join(f"{k}={v!r}" for k, v in refs.items())


def build_explanation(
    row: RequestRow, runs: list[dict[str, Any]], transitions: list[dict[str, Any]], reviews: list[dict[str, Any]]
) -> dict[str, Any]:
    triggered: list[dict[str, Any]] = []
    passed: list[str] = []
    skipped: list[str] = []
    dependencies: list[dict[str, Any]] = []
    derived: dict[str, Any] = {}
    routing: list[dict[str, Any]] = []

    for run in runs:
        t = run["trace"]
        if run["stage_type"] == "rules":
            for r in t.get("rules", []):
                if r["outcome"] == "PASS":
                    passed.append(r["rule_id"])
                elif r["outcome"] == "SKIPPED":
                    skipped.append(r["rule_id"])
                else:
                    triggered.append({"stage": run["stage_id"], **r})
        elif run["stage_type"] == "dependency":
            dependencies.append(
                {
                    "stage": run["stage_id"],
                    "dependency": t.get("dependency"),
                    "outcome": run["outcome"],
                    "attempts": len(t.get("attempts", [])),
                    "error": t.get("error"),
                    "response": t.get("response"),
                }
            )
        elif run["stage_type"] == "compute":
            derived.update(t.get("outputs", {}))
        elif run["stage_type"] == "branch":
            routing.append({"stage": run["stage_id"], "selected": t.get("selected"), "next": run["next_target"]})

    decision = row.decision or {}
    lines = [
        f"Request {row.id} ({row.workflow} v{row.workflow_version}) is {row.status.value}.",
    ]
    if decision:
        lines.append(
            f"Decision {decision.get('outcome')} made by {decision.get('decided_by')} at stage "
            f"'{decision.get('decided_at_stage')}' on {decision.get('decided_at')}."
        )
    if derived:
        lines.append("Derived values: " + ", ".join(f"{k}={v!r}" for k, v in derived.items()) + ".")
    for d in dependencies:
        if d["outcome"] == "pass":
            lines.append(f"External check '{d['dependency']}' succeeded after {d['attempts']} attempt(s).")
        else:
            lines.append(
                f"External check '{d['dependency']}' {d['outcome']} after {d['attempts']} attempt(s): {d['error']}."
            )
    for r in routing:
        lines.append(f"Routing at '{r['stage']}' selected '{r['selected']}' -> {r['next']}.")
    if triggered:
        lines.append("Rules that affected the outcome:")
        for i, r in enumerate(triggered, 1):
            lines.append(
                f"  {i}. [{r['rule_id']}] {r['name']} -> {r['outcome']} ({r['action']}): {r['reason']} "
                f"| data: {_fmt_refs(r['data_refs'])}"
            )
    else:
        lines.append("No rule failed.")
    lines.append(f"{len(passed)} rule(s) passed, {len(skipped)} not applicable.")
    for rv in reviews:
        lines.append(f"Manual review by {rv['reviewer']}: {rv['decision']} ({rv['comment']}).")

    return {
        "request_id": row.id,
        "workflow": row.workflow,
        "workflow_version": row.workflow_version,
        "definition_hash": row.definition_hash,
        "evaluation_date": row.evaluation_date,
        "status": row.status.value,
        "decision": decision,
        "input": row.context.get("input"),
        "path": [
            {"stage": r["stage_id"], "type": r["stage_type"], "outcome": r["outcome"], "next": r["next_target"]}
            for r in runs
        ],
        "rules_triggered": triggered,
        "rules_passed": passed,
        "rules_not_applicable": skipped,
        "derived_values": derived,
        "dependencies": dependencies,
        "routing": routing,
        "reviews": reviews,
        "lifecycle": transitions,
        "narrative": "\n".join(lines),
    }
