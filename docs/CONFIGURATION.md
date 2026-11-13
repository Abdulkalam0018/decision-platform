# Configuration model

A workflow is one YAML (or JSON) document. Everything business-specific lives in it: the input contract,
thresholds, rules, routing, external calls, retry budgets and duplicate policy. The engine code is the same for
all five shipped workflows.

| File | Use case | Highlights |
|---|---|---|
| `config/workflows/loan_application.v1.yaml` | Application approval | mandatory, threshold, conditional rules; derived ratios; 2 dependencies; branch; high-value multi-step stage; flags |
| `config/candidates/loan_application.v2.yaml` | Rule change | tighter thresholds and a new conditional rule; published at runtime, not loaded at startup |
| `config/workflows/insurance_claim.v1.yaml` | Claim processing | date rules on pinned evaluation date; permanent error rejects; computed payout; value routing |
| `config/workflows/employee_onboarding.v1.yaml` | Employee onboarding | list functions; work-permit conditional; privileged-access routing; duplicate `reject` |
| `config/workflows/vendor_approval.v1.yaml` | Vendor approval | regex (GSTIN); sanctions screening; `first_failure` evaluation |
| `config/workflows/document_verification.v1.yaml` | Document verification | aggregate over a document list; expiry window |

## 1. Top-level structure

```yaml
workflow: loan_application        # [a-z][a-z0-9_]*, at most 64 characters
version: 1                        # integer >= 1; (workflow, version) is immutable once published
owner: retail-credit-risk         # informational
description: ...
input_schema: {...}               # JSON Schema (Draft 2020-12) for the request body
parameters: {...}                 # named constants referenced as params.<name>
dependencies: {...}               # external services this workflow may call
duplicate_check: {...}            # optional business-level duplicate detection
retry: {...}                      # request-level retry budget after a dependency outage
start: eligibility                # optional, defaults to the first stage
stages: [...]                     # the workflow graph (must be a DAG)
```

Unknown keys are rejected anywhere in the document (typos such as `nxt:` fail validation instead of being ignored).

### `input_schema`

Standard JSON Schema. Use `additionalProperties: false` to reject unexpected fields, `required`, `enum`,
`pattern`, `minimum` and so on. Violations are returned together as HTTP 422 with field paths.

### `parameters`

```yaml
parameters:
  min_credit_score: 620
  supported_countries: [IN, US, GB, SG]
```

Rules reference `params.min_credit_score`. The compiler rejects any reference to a parameter that does not exist,
so a renamed threshold cannot silently evaluate to "missing". Changing a policy number is a one-line change in a
new version.

### `dependencies`

```yaml
dependencies:
  credit_bureau:                  # local name, used by dependency stages
    type: credit_bureau           # registered adapter type (see below)
    timeout_ms: 800
    retry: {max_attempts: 3, base_delay_ms: 50, max_delay_ms: 400}       # inline retries, full jitter
    circuit_breaker: {failure_threshold: 5, reset_timeout_s: 30}
```

Defaults when omitted: `timeout_ms: 1000`, `retry: {max_attempts: 3, base_delay_ms: 100, max_delay_ms: 2000}`,
`circuit_breaker: {failure_threshold: 5, reset_timeout_s: 30}`. Circuit breakers are keyed by
`<workflow>:<dependency>`, so these thresholds apply to this workflow only; a new version with different thresholds
updates its breaker (trade-off in ADR-10).

Registered simulated adapter types: `credit_bureau`, `fraud_screen`, `document_verifier`, `policy_lookup`,
`background_check`, `sanctions_screen`, `notifier`. A real integration is one adapter registered in `DependencyHub`;
workflows then refer to it by type.

### `duplicate_check`

```yaml
duplicate_check:
  fields: [applicant.id, loan.purpose, loan.amount]   # paths in the input
  window_hours: 24
  action: return_existing          # or: reject (HTTP 409)
```

Independent of the `Idempotency-Key`. The key is a truncated SHA-256 of the listed field values. If any field is
absent, no business key is computed. Requests that ended in `FAILED` (a technical failure) are not counted, so they
can be resubmitted. Defaults: `window_hours: 24`, `action: return_existing`.

### `retry`

```yaml
retry:
  max_request_retries: 3           # how many times a parked request is resumed
  base_delay_s: 60                 # exponential, jittered in [ceiling/2, ceiling]
  max_delay_s: 900
  on_exhausted: manual_review      # or: failed
```

Defaults when omitted: 3 retries, `base_delay_s: 30`, `max_delay_s: 900`, `on_exhausted: manual_review`.

## 2. Stages

Every stage has an `id` (`[a-z][a-z0-9_]*`, at most 64 characters, not a terminal name) and an optional
`description`. Transition targets are either another stage id or a terminal: `approve`, `reject`,
`manual_review`.

### `rules`

```yaml
- id: risk_assessment
  type: rules
  evaluation: all                  # all (default) | first_failure
  rules: [...]
  next: routing                    # when no blocking rule fails
  on_review: manual_review         # default
  on_reject: reject                # default
```

The stage outcome is the most severe action among failed rules: `reject` over `review`; `flag` never blocks. With
`evaluation: all` every applicable rule is evaluated, so the explanation lists every reason (DMN "Collect" with a
max-severity aggregator). `first_failure` stops at the first blocking failure (DMN "First").

### `compute`

```yaml
- id: affordability
  type: compute
  outputs:
    dti: "round((input.applicant.monthly_debt if exists(input.applicant.monthly_debt) else 0) / input.applicant.monthly_income + input.loan.amount / input.loan.term_months / input.applicant.monthly_income, 4)"
  next: credit_check
```

Values are stored as `derived.<name>` and recorded in the trace. If an output cannot be computed (missing data,
division by zero) the request goes to `manual_review` with the reason.

### `dependency`

```yaml
- id: credit_check
  type: dependency
  dependency: credit_bureau        # local name from `dependencies`
  request:                         # field -> expression
    applicant_id: "input.applicant.id"
  output_key: credit               # response available to later stages as credit.<field>
  on_unavailable: retry_later      # retry_later | manual_review | reject   (transient errors after inline retries)
  on_error: manual_review          # manual_review | reject                 (permanent errors, e.g. 404 subject)
  next: fraud_check
```

The call carries the idempotency token `<request_id>:<stage_id>`. The response is stored in the request
context, so the stage is never re-executed once it has succeeded.

### `branch`

```yaml
- id: routing
  type: branch
  branches:                        # first match wins
    - label: high value loan
      when: "input.loan.amount > params.high_value_threshold"
      next: high_value_checks
    - label: elevated debt-to-income
      when: "derived.dti > params.review_dti"
      next: manual_review
  default: approve
```

## 3. Rules

```yaml
- id: RISK-001                     # unique within the workflow; appears in every explanation
  name: Minimum credit score
  kind: threshold                  # mandatory | threshold | conditional
  when: "..."                      # precondition; required for conditional rules; false => SKIPPED
  condition: "credit.score >= params.min_credit_score"   # must be truthy to PASS
  on_fail: reject                  # reject | review | flag
  on_missing: review               # optional; action if referenced data is absent
  reason: "Credit score {credit.score} is below the minimum {params.min_credit_score}"
```

| Field | Meaning |
|---|---|
| `kind` | Documents intent and sets defaults; default `mandatory`. `mandatory`: always applies; missing data uses `on_fail`. `threshold`: numeric limit, usually parameterised. `conditional`: requires `when` |
| `on_fail` | Default `reject`. `reject` routes to the stage's `on_reject` target (normally REJECTED), `review` to `on_review` (normally manual review), `flag` is recorded on the decision but never blocks |
| `on_missing` | Default: `on_fail` for mandatory rules, otherwise `review`. Prevents "absent data" from being silently treated as pass or fail |
| `reason` | Template; `{path}` placeholders are filled from the same context the rule saw |

Each evaluation is traced with outcome `PASS`, `FAIL`, `SKIPPED`, `MISSING_DATA` or `ERROR`, the action applied,
the rendered reason and `data_refs` (every variable read and its value).

## 4. Expression language

A small, safe subset of Python expression syntax, parsed with `ast` and interpreted against a whitelist.

### Namespaces

| Root | Contents |
|---|---|
| `input` | The validated request body |
| `params` | The workflow's `parameters` |
| `derived` | Values produced by `compute` stages |
| `<output_key>` | Response of a dependency stage, e.g. `credit`, `fraud`, `policy`, `docs` |
| `meta` | `request_id`, `workflow`, `version`, `retry_count`, `evaluation_date`, `submitted_at` |

### Operators

`+ - * / // %`, comparisons (chainable: `18 <= x < 65`), `in`, `not in`, `is`, `is not`, `and`,
`or`, `not`, conditional `a if cond else b`, list literals, indexing `docs[0]`, field access on indexed items
`docs[0].type`. Literals: numbers, strings, `true/false/null` (or Python spellings).

### Functions

| Function | Purpose |
|---|---|
| `exists(x)` | True if `x` is present and not null (the only function that can observe missing data) |
| `len, abs, min, max, round, floor, ceil` | As in Python (argument counts checked at load time) |
| `sum(list)` | Sum of a list of numbers only |
| `lower(s), upper(s)` | String case |
| `matches(s, regex)` | Full-match regular expression. The pattern must be a string literal or `params.<name>`; it is validated at load time (must compile, max 200 chars, no nested quantifiers such as `(a+)+`). Input strings over 1000 chars are refused |
| `days_since(date), days_until(date)` | Relative to the request's pinned evaluation date |
| `count_where(list, key, value)` | Count items whose `key` equals `value` |
| `all_where(list, key, value)` | True if the list is non-empty and every item matches |
| `pluck(list, key)` | List of `item[key]` |
| `safe_div(a, b, default)` | Division returning `default` when `b` is 0 |

### Limits

Rejected when the workflow is loaded: imports, `eval`/`open`/arbitrary calls, attribute access on
Python objects, names starting with `_`, lambdas, comprehensions, assignment, `**`, keyword arguments, bytes,
wrong argument counts, `is`/`is not` against anything other than `None`, expressions over 500 characters or nested
deeper than 32 levels.

Refused at runtime with an evaluation error, which the rule engine traces as `ERROR`: sequence repetition or
concatenation beyond 10,000 elements, integers beyond 10^18 in magnitude, `%` string formatting, more than 5,000
evaluation steps.

### Missing data

A reference to absent data raises inside evaluation and is handled by the rule's `on_missing`. `and`, `or` and
`if-else` short-circuit, so `exists(x) and x > 0` is safe.

## 5. Validation performed before a definition can be stored

1. Document structure and types (pydantic), unknown keys, reserved ids, conditional rules without `when`.
   Malformed YAML is reported as a validation error (HTTP 422), not a server error.
2. `input_schema` is a valid JSON Schema and uses only local `#/...` references (no network or file `$ref`);
   every `duplicate_check` field exists in its properties.
3. Graph: unique stage ids, start exists, every target exists, no cycles, no unreachable stages.
4. References: dependencies declared and their adapter type registered, `output_key` unique, rule ids unique,
   every `params.x` defined.
5. Every expression compiles in the sandbox; regex parameters are valid and safe; every `{placeholder}` in a rule
   `reason` has a known root and parameter.
6. Data flow: a stage may only read `derived.x` or `<output_key>.*` (in expressions or reason placeholders) if
   it is produced on every path to that stage.

All problems are reported together. A rejected definition is audited (`config.rejected`) and never activated.

## 6. Lifecycle of a change

```text
validate  ->  publish (immutable version)  ->  simulate / impact  ->  activate  ->  (rollback = activate previous)
```

| Step | REST | CLI |
|---|---|---|
| Validate only | `POST /v1/workflows/validate` (YAML body) | `dp workflows validate FILE` |
| Publish | `POST /v1/workflows?actor=..&reason=..` | `dp workflows publish FILE` |
| Dry run one payload | `POST /v1/workflows/{wf}/simulate` | `dp simulate WF FILE --version N` |
| Impact on history | `GET /v1/workflows/{wf}/impact?from_version=1&to_version=2` | `dp impact WF --from 1 --to 2` |
| Activate / roll back | `POST /v1/workflows/{wf}/versions/{v}/activate` | `dp workflows activate WF V` |

Impact analysis replays historical requests of the old version against the new one using their stored inputs and
recorded dependency responses, so it makes no external calls and is deterministic. It reports counts per
outcome transition (e.g. `APPROVED->REJECTED: 1`) and every request that would change, with the new reasons.

Republishing identical content under an existing version is a no-op; different content under an existing version
is refused (HTTP 409). In-flight requests finish on the version they started with.

## 7. Adding a new workflow (example)

```yaml
workflow: expense_claim
version: 1
input_schema:
  type: object
  required: [employee_id, amount, category, receipts]
  properties:
    employee_id: {type: string}
    amount: {type: number, exclusiveMinimum: 0}
    category: {enum: [travel, meals, equipment]}
    receipts: {type: array, items: {type: object}}
parameters: {meal_cap: 3000, auto_limit: 25000}
stages:
  - id: policy
    type: rules
    rules:
      - id: EXP-1
        name: Receipt attached
        condition: "len(input.receipts) >= 1"
        on_fail: reject
        reason: "No receipt attached"
      - id: EXP-2
        name: Meal cap
        kind: conditional
        when: "input.category == 'meals'"
        condition: "input.amount <= params.meal_cap"
        on_fail: review
        reason: "Meal claim {input.amount} exceeds cap {params.meal_cap}"
    next: route
  - id: route
    type: branch
    branches:
      - when: "input.amount > params.auto_limit"
        next: manual_review
    default: approve
```

`dp workflows publish expense_claim.yaml --activate` validates, stores and activates it. No code change is needed.

## 8. What still needs code

| Change | Code needed |
|---|---|
| New workflow, rules, thresholds, routing | None (YAML) |
| New external system | One adapter function registered in `DependencyHub` |
| New kind of stage (e.g. scoring model, human task with SLA) | One pydantic stage model + one handler in `HANDLERS` |
| New expression function | One entry in `rules/expression.py` `_functions` |
