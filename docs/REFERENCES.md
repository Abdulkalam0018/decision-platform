# References

Sources behind specific design decisions, and where each is applied.

| Topic | Source | Applied in |
|---|---|---|
| Idempotency-Key header semantics (fingerprint, 422 on key reuse with a different payload, 409 while the original is in flight, key expiry) | J. Jena, S. Dalal, *The Idempotency-Key HTTP Header Field*, IETF draft-ietf-httpapi-idempotency-key-header-07 (2025). https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/ | `DecisionService.submit`, API error mapping, ADR-8 |
| Idempotency keys on mutating endpoints, safe client retries | B. Leach, *Designing robust and predictable APIs with idempotency*, Stripe Engineering. https://stripe.com/blog/idempotency ; Stripe API docs, *Idempotent requests*. https://docs.stripe.com/api/idempotent_requests | Transport idempotency layer |
| Client request tokens, payload validation for repeated tokens, auditability of intent | M. Featonby, *Making retries safe with idempotent APIs*, Amazon Builders' Library. https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/ | Dependency idempotency token `<request_id>:<stage_id>` |
| Capped exponential backoff with full jitter | M. Brooker, *Exponential Backoff And Jitter*, AWS Architecture Blog (2015). https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/ | `resilience/policies.py` `BackoffPolicy` |
| Retry at a single layer, only retry idempotent operations, cap attempts | M. Brooker, *Timeouts, retries, and backoff with jitter*, Amazon Builders' Library. https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/ ; AWS Well-Architected REL05-BP03 | ADR-10 |
| Circuit breaker (closed, open, half-open) | M. Fowler, *CircuitBreaker* (bliki). https://martinfowler.com/bliki/CircuitBreaker.html ; M. Nygard, *Release It!* | `resilience/policies.py` `CircuitBreaker` |
| Transactional outbox, dual-write problem | C. Richardson, *Pattern: Transactional outbox*, microservices.io. https://microservices.io/patterns/data/transactional-outbox.html | `outbox` table, `DecisionService.relay_outbox`, SCALING.md (CDC) |
| Saga / compensations (considered, not needed for single-store decisions) | C. Richardson, *Pattern: Saga*. https://microservices.io/patterns/data/saga.html ; Microsoft Azure Architecture Center, *Saga pattern*. https://learn.microsoft.com/azure/architecture/patterns/saga | ADR-7 alternatives |
| Durable execution via event history and stored activity results | Temporal documentation, *Event History*. https://docs.temporal.io/encyclopedia/event-history/ | ADR-7 checkpoint design |
| Event sourcing | M. Fowler, *Event Sourcing* (2005). https://martinfowler.com/eaaDev/EventSourcing.html | ADR-7 alternatives |
| Decision table hit policies (Unique, First, Collect with aggregators) | OMG *Decision Model and Notation*; Camunda docs, *Hit policy*. https://docs.camunda.io/docs/components/modeler/dmn/decision-table-hit-policy/ ; Calvanese et al., *Semantics and Analysis of DMN Decision Tables*, arXiv:1603.07466 | ADR-5, `rules/engine.py` |
| Tamper-evident logging: hash chains, Merkle history trees | S. Crosby, D. Wallach, *Efficient Data Structures for Tamper-Evident Logging*, USENIX Security 2009; hash-chain construction after Kelsey and Schneier | `audit/audit_log.py`, ADR-9, SCALING.md |
| SQLite WAL: readers and writer concurrent, single writer | SQLite documentation, *Write-Ahead Logging*. https://www.sqlite.org/wal.html | ADR-2, `persistence/db.py` |
| Problem details for HTTP APIs | RFC 9457 (obsoletes RFC 7807) | API error responses |
| JSON Schema Draft 2020-12 | https://json-schema.org/draft/2020-12 | Intake validation |
