# Trade-offs and design decisions

Each decision is a short ADR: the decision, the alternatives considered, what it costs, and where relevant the
condition under which to revisit it. Sources are in [REFERENCES.md](REFERENCES.md).

## ADR-1 Synchronous execution with an asynchronous fallback

**Context.** Most decisions finish in milliseconds. A few hit a slow or failing dependency.
**Decision.** `POST /v1/workflows/{wf}/requests` runs the workflow inline and returns the decision. If a
dependency is unavailable after bounded inline retries, the request is parked (`RETRY_PENDING`) and a worker resumes it later; the client polls
`GET /v1/requests/{id}` or consumes the outbox notification.
**Alternatives.** Queue-first (always 202 Accepted, process in workers): better isolation and throughput under load,
but every client must poll or subscribe even in the common fast case, and the system needs a broker.
**Consequences.** Clients get an answer in the same call. HTTP latency includes inline retry time, so inline budgets
are kept to 2 or 3 attempts with sub-second backoff, and longer outages are handled by parking.
**Revisit when** p99 latency or request volume makes inline execution expensive: switch the API to 202 and let
the existing worker path (already implemented and tested) do all processing.

## ADR-2 SQLite in WAL mode

**Context.** The deliverable must run anywhere with one command, yet demonstrate real transactional guarantees.
**Decision.** SQLite in WAL mode, one connection per thread, `BEGIN IMMEDIATE` per unit of work, optimistic
concurrency (`lock_version`) on request rows. Request and audit SQL is in `persistence/` and `audit/`;
`service.py` (idempotency keys, queries, outbox), `config/registry.py` and the engine's lease heartbeat also issue
plain SQL.
**Alternatives.** PostgreSQL (multi-writer, `SELECT ... FOR UPDATE SKIP LOCKED` work queues, replicas) at the cost
of a second process to run; an in-memory store (no durability, so partial-save tests would be meaningless).
**Consequences.** Real ACID transactions and crash safety with zero setup. SQLite allows one writer at a time,
which caps write throughput (measured 230 to 290 requests/s on 2 vCPUs, about 12 audit rows each; see
[SCALING.md](SCALING.md)) and rules out multi-node writers.
**Revisit when** more than one API node must write, or sustained load approaches 100 req/s (well below the measured
ceiling, to leave headroom).
The migration is contained: the statements are plain SQL with `?` placeholders in a handful of modules,
and the workflow and rule logic does not change.

## ADR-3 A sandboxed expression language instead of `eval`, JSON-logic or full DMN/FEEL

**Context.** Business users need readable conditions; the platform must not execute arbitrary code from config.
**Decision.** Python-like expressions (`credit.score >= params.min_credit_score`) parsed with `ast` and interpreted
against a node and function whitelist, with size/depth limits, runtime guards on sequence repetition and number
magnitude, and every variable read recorded for the explanation.
**Alternatives.**
* `eval` with restricted globals: well known to be escapable; rejected.
* JSON-logic: safe and portable but unreadable in YAML (`{">=": [{"var": "credit.score"}, 620]}`), and error
  messages are poor.
* DMN decision tables with FEEL (Camunda, Drools): a standard with strong tooling and hit policies, but heavy to
  embed and harder to diff in code review. We borrowed DMN's hit-policy semantics instead (ADR-5).
* Google CEL: a good fit (safe, typed, linear time) but adds a native dependency; a reasonable future swap
  because the compile/evaluate interface is already isolated.
**Consequences.** Readable rules, compile-time rejection of unsafe or misspelled expressions, precise data
references. We maintain our own interpreter (`rules/expression.py`, under 500 lines, about 50 test cases plus a
fuzz test).

## ADR-4 Workflows are DAGs (no cycles)

**Decision.** The compiler rejects cycles. **Why.** Termination is guaranteed (at most one execution per stage
per run), the "available data on every path" check is a simple topological pass, and the explanation is a linear path.
**Alternative.** Allow loops with iteration limits (BPMN style) for "request more documents" flows. **Cost.**
Loops must be modelled as manual review or a new linked request. **Revisit when** a real process needs bounded
re-checks; add a `max_visits` per stage and keep the termination guarantee.

## ADR-5 Collect-all rule evaluation with severity aggregation

**Decision.** By default every applicable rule in a stage is evaluated and the most severe action wins
(`reject` > `review` > `flag`). This is DMN's *Collect* hit policy with a MAX aggregator. `first_failure`
(DMN *First*) is available per stage.
**Why.** A rejected applicant, an auditor and support staff need every reason, not just the first one found.
**Cost.** Slightly more evaluation work; rules must be side-effect free (they are, by construction).

## ADR-6 Immutable versions, explicit activation, pinning

**Decision.** `(workflow, version)` is immutable (content-hash enforced). Activation is a separate audited step.
Each request stores the version and content hash it started with and is always executed and explained against it.
**Alternatives.** Hot-editing rules in place (simple, but past decisions become unexplainable and in-flight
requests change behaviour mid-way); feature flags per rule (finer grained, but combinatorial and hard to audit).
**Consequences.** Safe rollouts, one-step rollback, deterministic replay, and impact analysis before activation.
Old versions must be kept forever (they are small).

## ADR-7 Stage checkpoints instead of full event sourcing or an external workflow engine

**Context.** Durable execution needs resume after a crash, no repeated side effects, and full history.
**Decision.** After each stage, one transaction stores the stage run (trace + context patch), the updated request
row and the audit events. Dependency responses are persisted in the context so completed stages are never re-run;
the in-flight stage is re-run with the same idempotency token.
**Alternatives.**
* Temporal / Cadence: event-sourced durable execution with replay and timers. Production-grade, but requires
  running a cluster and writing deterministic workflow code; the configuration-driven requirement still needs an
  interpreter on top.
* Camunda / Zeebe with BPMN + DMN: strong modelling tools, heavier operational footprint.
* Pure event sourcing: current state derived by replaying events. A natural audit trail, but projections,
  snapshots and event versioning add complexity that a decision service does not need.
* Saga with compensating actions: not needed here. Each decision writes to one database, every dependency call
  carries an idempotency token so repeating it is harmless, and the notification goes through the transactional
  outbox.
**Consequences.** Most of the durability benefits with a small, inspectable implementation. We keep both a mutable
current-state row (fast reads) and an append-only history (stage_runs, transitions, audit), similar to how
Temporal keeps "mutable state" alongside event history.
**Revisit when** workflows become long-running (days), need timers or human tasks with SLAs; Temporal is then the
natural host and the stage handlers can become activities.

## ADR-8 Idempotent replay returns the current state of the same request

**Decision.** A repeated `POST` with the same key and payload returns the *current* view of the original request
with `Idempotent-Replayed: true` and HTTP 200, rather than a byte-for-byte copy of the first response.
**Alternatives.** Stripe stores and replays the exact first response, which is ideal for one-shot operations.
**Why ours.** A request may legitimately move from `RETRY_PENDING` to `APPROVED` between attempts; returning the
stale first response would make the client think it is still pending. No side effect is repeated either way,
which is what idempotency requires. Same key with a different payload returns 422 and a concurrent duplicate
returns 409, following draft-ietf-httpapi-idempotency-key-header. Payloads are fingerprinted after normalisation
(key order, `600000` vs `600000.0`). If a caller takes over an expired lease but another worker still owns the
request, it receives 202 and the key stays open, so no caller ever receives a frozen `IN_PROGRESS` "final" answer.

## ADR-9 Hash-chained audit log in the same database transaction

**Decision.** Audit events are rows in the same SQLite database, written in the same transaction as the state they
describe, with a SHA-256 hash chain.
**Why.** No dual-write problem: the audit trail cannot drift from the state it describes, and edits to either are
detectable.
**Cost.** A single global chain serialises audit writes (fine with one writer anyway) and verification is linear
in log size (150 to 190 ms for 12k events in our benchmark runs). Rewriting the whole chain consistently, or
truncating its newest events, can only be detected by anchoring the head hash outside the database.
**Revisit when** scaling out: per-request or per-partition chains, periodic anchoring of head hashes to WORM
storage, or a Merkle history tree (Crosby and Wallach) for logarithmic proofs. See SCALING.md.

## ADR-10 Retries at a single layer, two time scales

**Decision.** Short inline retries (2-3 attempts, capped exponential backoff with full jitter) inside the dependency
stage; long retries (minutes) by parking the request. Only transient errors are retried; permanent errors never are.
A circuit breaker per workflow and dependency makes an outage cost one fast failure per request instead of a full
retry budget.
**Why.** Retrying at several layers multiplies load (Amazon Builders' Library); jitter prevents synchronised retry
waves (AWS Architecture Blog). Parking keeps the HTTP request short and keeps state durable.
**Breaker scope.** Breakers are keyed per `workflow:dependency` so each workflow's configured thresholds apply to
its own calls. The cost is that two workflows calling the same service learn about an outage separately (a few extra
failed calls). The alternative, one breaker per external service with a single global configuration, is better at
scale and is a one-line key change once dependency configuration moves out of workflow files.
**Cost.** Breaker state is per process (acceptable: each instance protects itself); a shared breaker would need
Redis or similar.

## ADR-11 Business duplicate detection separate from idempotency keys

**Decision.** Optional per workflow: hash of configured input fields within a time window; `return_existing` or `reject`.
**Why.** Idempotency keys protect against transport retries only. Users and upstream systems re-submit with fresh
keys; claims systems must not pay the same claim twice. **Cost.** A legitimate second identical request inside the
window is treated as a duplicate; the window and fields are configurable per workflow for that reason.

## ADR-12 Build vs buy

A production team should consider Camunda (BPMN + DMN), Temporal, or a rules engine such as Drools. This
submission builds a compact engine because the assignment evaluates engineering judgement and the internal design,
and because it keeps the system runnable with `pip install` and no infrastructure. The internal boundaries (stage
handlers, dependency adapters, compile/evaluate interface, repository) are drawn where a migration to those
products would cut.

## Known limitations

* Single writer; no horizontal write scaling without moving to PostgreSQL.
* No authentication or authorisation; admin and publish endpoints must be protected by a gateway.
* Circuit breaker and simulated dependency fault plans are in-memory per process.
* Manual review is terminal; there is no "send back for more information" loop.
* The idempotency TTL (24 h) is a constant. The lease is derived per workflow from its worst-case stage time and
  renewed at each checkpoint.
* ReDoS protection is a conservative heuristic (nested quantifiers refused, input length capped) because Python's
  `re` has no timeout; production should use RE2.
* The audit chain detects edits to audit rows and to request outcomes, but not a consistent rewrite of the whole
  chain or truncation of its newest events; external anchoring of the head hash closes that gap (ADR-9).
* The outbox is at-least-once: consumers must deduplicate by `request_id` and event type.
