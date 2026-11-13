# Configurable Workflow Decision Platform

A configuration-driven decision engine. It validates incoming requests, evaluates versioned business rules, runs a
workflow graph that can call external dependencies, records every state change, and returns a decision. Submissions
are idempotent and safe to retry, a crash mid-workflow resumes from the last completed stage, each decision records
which rules fired on which data, and every state change is written to a hash-chained audit log.

Five workflows run on the same engine with no workflow-specific engine code: loan application approval, insurance
claim processing, employee onboarding, vendor approval and document verification.

```mermaid
flowchart LR
    req["POST request"] --> schema["Schema validation"] --> idem["Idempotency and duplicate check"] --> graph["Stage graph: rules, compute, dependency, branch"]
    graph -->|"one checkpoint per stage"| graph
    graph --> out["APPROVED / REJECTED / MANUAL_REVIEW / RETRY_PENDING / FAILED"]
    out --> rec["Explanation, lifecycle history, audit chain, outbox event"]
```

## Quick start

Requires Python 3.10+.

```bash
pip install -e ".[dev]"          # or: pip install -r requirements.txt && export PYTHONPATH=src
pytest                           # 224 tests, ~96% line coverage
make lint                        # ruff, ruff format --check, mypy
dp serve                         # REST API on http://127.0.0.1:8000 (Swagger UI at /docs)
./scripts/demo.sh                # in another terminal: end-to-end walkthrough with curl
```

Docker: `docker build -t decision-platform . && docker run -p 8000:8000 decision-platform`

### Try it

```bash
curl -s -X POST localhost:8000/v1/workflows/loan_application/requests \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  --data @examples/requests/loan_reject_credit.json

curl -s "localhost:8000/v1/requests/<request_id>/explanation?format=text"
```

```text
Request 5f0c... (loan_application v1) is REJECTED.
Decision REJECTED made by engine at stage 'risk_assessment' on 2026-11-13T09:00:00.000+00:00.
Derived values: dti=0.1333, ltv=None, monthly_installment=10000.0.
External check 'credit_bureau' succeeded after 1 attempt(s).
External check 'fraud_screen' succeeded after 1 attempt(s).
Rules that affected the outcome:
  1. [RISK-001] Minimum credit score -> FAIL (reject): Credit score 541 is below the minimum 620 | data: credit.score=541, params.min_credit_score=620
  2. [RISK-002] No open defaults -> FAIL (review): Bureau reports 2 open default(s) | data: credit.open_defaults=2
  ...
```

### CLI

```bash
dp workflows list
dp submit loan_application examples/requests/loan_approve.json --key k-1
dp explain <request_id>
dp history <request_id>            # transitions and every stage run with its trace
dp review <request_id> --approve --reviewer kim --comment "salary verified"
dp workflows publish config/candidates/loan_application.v2.yaml
dp impact loan_application --from 1 --to 2       # which past decisions would change?
dp workflows activate loan_application 2
dp worker --once                    # resume due retries, recover stale requests, relay outbox
dp audit-verify
```

`DP_DB_PATH` (default `data/decisions.db`) and `DP_CONFIG_DIR` (default `config/workflows`) apply to both the API
and the CLI.

## REST API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/workflows/{wf}/requests` | Submit (requires `Idempotency-Key`). 201 new; 200 replay, lease takeover or business duplicate; 202 still being processed by another worker; 400 missing key; 422 invalid input or key reused with a different payload; 409 in flight or duplicate with `action: reject`; 404 unknown workflow |
| GET | `/v1/requests`, `/v1/requests/{id}` | List (filter by workflow, status) and fetch |
| GET | `/v1/requests/{id}/history` | Lifecycle transitions, stage runs with traces, reviews |
| GET | `/v1/requests/{id}/explanation[?format=text]` | Structured or plain-language explanation |
| GET | `/v1/requests/{id}/audit` | Hash-chained audit events for the request |
| POST | `/v1/requests/{id}/retry` | Resume a `RETRY_PENDING` request now |
| POST | `/v1/requests/{id}/review` | Manual review decision `{decision, reviewer, comment}` |
| GET | `/v1/workflows`, `/v1/workflows/{wf}` | Versions, active version, activation history, definition |
| POST | `/v1/workflows/validate`, `/v1/workflows` | Validate or publish a YAML/JSON definition |
| POST | `/v1/workflows/{wf}/versions/{v}/activate` | Activate or roll back |
| POST | `/v1/workflows/{wf}/simulate` | Dry run a payload against any version |
| GET | `/v1/workflows/{wf}/impact?from_version&to_version` | Replay history against another version |
| POST | `/v1/admin/retries/run`, `/v1/admin/outbox/relay` | One worker tick (also `dp worker`) |
| GET | `/v1/admin/dependencies` | Dependency call counts, breaker state, fault plans |
| PUT | `/v1/admin/dependencies/{type}/faults` | Fault injection for a simulated dependency |
| POST | `/v1/admin/dependencies/reset` | Reset fault plans, call counters and breakers |
| GET | `/v1/admin/audit/verify` | Verify the audit hash chain |
| GET | `/health`, `/ready` | Liveness and readiness |

Errors use `application/problem+json` (RFC 9457).

## How the requirements are met

Test files are in `tests/`.

| Requirement | Implementation | Evidence |
|---|---|---|
| Intake and schema validation | JSON Schema per workflow, all violations reported, rejected intake audited by hash | `test_invalid_input.py` |
| Mandatory, threshold and conditional rules; branching; multi-stage flows | Rule kinds, `when` preconditions, `branch` stages, multiple rule stages, `compute` stages for derived values | `test_rule_outcomes.py` |
| Approve, reject, retry, manual review | Terminals `approve`, `reject`, `manual_review`; `RETRY_PENDING` resumed by the worker; review endpoint | `test_retry_flow.py`, `test_manual_review_and_state.py` |
| Lifecycle and change history | Explicit state machine, `transitions` table, `stage_runs` with context patches | `test_happy_path.py` |
| Explainable decisions | Per-rule trace with every variable read and its value; plain-language narrative | [DECISION_EXAMPLES.md](docs/DECISION_EXAMPLES.md) |
| Dependency failures and retries | Full-jitter backoff, circuit breaker, permanent vs transient errors, parking with a retry budget | `test_dependency_failure.py` |
| Partial save failures | One transaction per stage; fault injection inside transactions | `test_partial_save.py` |
| Duplicates and idempotency | Idempotency keys with fingerprint and lease, business duplicate keys, dependency idempotency tokens | `test_idempotency.py` |
| Configurability | Versioned YAML, static validation including data flow, dry run, impact analysis, activation and rollback | `test_config_validation.py`, `test_rule_change.py` |
| Simulated dependencies | 7 deterministic adapters with fault plans (transient, timeout, permanent, down, latency, failure rate) | `src/decision_platform/dependencies/simulated.py` |
| Audit log | Hash-chained, written in the same transaction as state, verification endpoint | `test_audit_and_explain.py` |
| Scaling | Measured baseline, bottlenecks, target architecture | [SCALING.md](docs/SCALING.md) |

## Documentation

* [Architecture](docs/ARCHITECTURE.md): problem framing, scope, assumptions, components, data model, state machine,
  data flow, consistency, failure handling
* [Configuration](docs/CONFIGURATION.md): YAML reference, expression language, validation, change lifecycle
* [Decision examples](docs/DECISION_EXAMPLES.md): 13 generated examples (input, rules triggered, output, audit trail)
* [Trade-offs](docs/TRADEOFFS.md): 12 ADRs and known limitations
* [Scaling](docs/SCALING.md): measured baseline and path to scale
* [Testing](docs/TESTING.md): scenario-to-test map
* [Hardening log](docs/HARDENING.md): 10 issues found in a second review of the code, each fixed with a regression test
* [References](docs/REFERENCES.md)

## Project layout

```text
config/workflows/        active workflow definitions (5 workflows)
config/candidates/       loan_application v2, used for the rule-change demo (published at runtime)
src/decision_platform/
  api/app.py             REST API (FastAPI)
  cli.py                 CLI (click)
  service.py             intake, idempotency, duplicates, review, retry, outbox, dry run, impact analysis
  workflow/              engine (checkpointed execution), stage handlers, simulator
  rules/                 sandboxed expression language, rule-set evaluation
  config/                schema, static compiler, versioned registry
  persistence/           SQLite unit of work, repository with CAS updates
  audit/                 hash-chained audit log
  resilience/            backoff with jitter, retry, circuit breaker
  dependencies/          simulated external services with fault injection
  explain/               explanation builder
  domain/                state machine, clock
tests/                   224 tests, including a Hypothesis fuzz test
examples/requests/       sample payloads
scripts/                 demo.sh, generate_examples.py, benchmark.py
```
