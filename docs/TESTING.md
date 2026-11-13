# Testing

```bash
pytest                                                   # 224 tests, under 20 s
pytest --cov=decision_platform --cov-report=term-missing # ~96% line coverage
```

Tests run against a real SQLite file per test (no mocks), the shipped YAML workflows, a `FixedClock` for
deterministic time, a recording `sleep` (backoff delays are asserted without waiting) and seeded randomness.
Failures come from the simulated dependencies' fault plans and from named fault-injection points inside database
transactions (`after_stage_run`, `after_stage_audit`, `after_request_update`, `before_commit`). The 224 collected
tests come from 164 test functions; the rest are parametrised cases.

## Required scenarios

| Assignment scenario | Where | What is asserted |
|---|---|---|
| Happy path | `test_happy_path.py` | All 5 workflows approve a clean request; path, stage runs, derived values, context, transitions `RECEIVED -> IN_PROGRESS -> APPROVED`, outbox event, valid audit chain |
| Invalid input | `test_invalid_input.py` | Missing/wrong-type/unknown/enum/pattern violations reported together with field paths; non-object payloads; no request or key persisted; `intake.rejected` audited with a payload hash only; missing or oversized `Idempotency-Key`; unknown workflow |
| Duplicate requests | `test_idempotency.py` | Same key replays without a second side effect; key order irrelevant; different payload -> 422; keys scoped per workflow; in-flight -> 409; 8 concurrent threads produce exactly 1 request and 1 bureau call; lease takeover after a crash; TTL expiry; business duplicate `return_existing` and `reject`; duplicate window |
| Dependency failure | `test_dependency_failure.py` | Transient + timeout recovered inline with jitter bounds checked; latency above timeout; exhausted retries park the request; permanent errors not retried and routed per config (review vs reject); circuit opens and fails fast without calling the dependency; half-open recovery; permanent errors do not trip the breaker; `on_unavailable: manual_review` |
| Retry flow | `test_retry_flow.py` | Manual retry resumes and completes; completed stages are not re-run (credit bureau called once); worker processes only due requests; backoff grows; budget exhaustion -> `MANUAL_REVIEW` or `FAILED`; retry refused for non-pending requests; worker recovers a request stuck `IN_PROGRESS` after a crash without a second bureau pull |
| Rule change | `test_rule_change.py` | v2 activation changes new decisions; historical decisions and explanations unchanged and pinned to v1's hash; in-flight request finishes on v1; new conditional rule only in v2; rollback; publish without activation; invalid v3 never active; dry run has no side effects; dry run with recorded outputs makes no calls; impact analysis identifies exactly the changed decisions without live calls |

## Additional depth

| Area | Where | Highlights |
|---|---|---|
| Partial save failures | `test_partial_save.py` | Crash at each of 4 points inside a stage commit: stage runs, current stage and audit events always agree; recovery completes exactly once (dependency side effect not repeated, single outbox message); crash during intake persists nothing; crash on the terminal step emits no decision; review rollback |
| Rule semantics | `test_rule_outcomes.py` | Collect-all reasons; reject beats review; non-blocking flags; conditional skip; `on_missing`; threshold on dependency data; branching; multi-step high-value path; LTV conditional; claims date rules on the pinned date; theft rule; payout branches; onboarding, vendor, KYC specifics; evaluation errors contained |
| Configuration validation | `test_config_validation.py` | Every shipped file compiles; unknown target, cycle, unreachable stage, unknown parameter, undeclared/unregistered dependency, bad expression, data-flow violations (dependency and derived), duplicate rule ids, invalid JSON Schema, reserved ids, missing `when`, unknown fields, all errors reported together, version immutability, rejected config audited, activation survives restart |
| Expression sandbox | `test_expression.py` | 22 evaluation cases; data references; missing data; short-circuit; 15 forbidden constructs (imports, dunder, lambdas, `**`, comprehensions, walrus, getattr, depth, length and others); runtime limits on sequence repetition and number size |
| Audit and explanation | `test_audit_and_explain.py` | Complete ordered event trail; tampering by modification and by deletion detected at the right row; explanation contains input, condition, data refs, dependency response, path, lifecycle; config lifecycle audited; evaluation date pinned |
| Manual review and state machine | `test_manual_review_and_state.py` | Approve/reject with justification; engine recommendation preserved; second review refused; illegal transitions raise; terminal states have no exits; every state reachable |
| Resilience primitives and outbox | `test_resilience_and_outbox.py` | Full-jitter bounds and spread; no retry on permanent; breaker state sequence; outbox at-least-once with retry |
| REST API | `test_api.py` | Status codes, `Location`, `Idempotent-Replayed`, problem+json bodies, every read endpoint, fault injection + retry + review over HTTP, publish/validate/impact/simulate/activate, version conflict 409, ops endpoints, unexpected error -> 500 problem |
| Hardening (second review) | `test_hardening.py` | Error containment in every expression site and in handlers; poison-request isolation in the worker; dry-run isolation; sandbox resource guards; Hypothesis fuzz test of the interpreter (400 generated expressions); compiler gaps; tamper detection on the `requests` table; per-workflow breakers; intake data minimisation; HTTP edge cases; lease heartbeat and resume claims; FAILED resubmission. See [HARDENING.md](HARDENING.md) |
| CLI | `test_cli.py` | Every command except `serve` against a real database, including review and rule-change commands and error exit codes |
