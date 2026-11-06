# Domain rules: an example

The `rust` skill covers the language. Domain rules are the project's own: a web API, a job
queue, payments. This file shows what such rules look like for a typical service (Axum +
tokio + sqlx, one binary with a queue worker, Postgres, a payment provider). It is an
example, not rules for your project — write your own domain skill and keep the parts that
fit.

## HTTP boundary as an anti-corruption layer

- An Axum extractor turns raw input into a typed value once: a signed webhook body becomes
  `VerifiedWebhook` with the signature and timestamp already checked, not a
  `HashMap<String, String>`.
- A provider's reply → provider DTO → domain model. Provider fields never reach the
  domain.
- Errors at the boundary: `ApiError { code, message, details, request_id }` implementing
  `IntoResponse`. Mapping domain errors to codes and statuses happens only there.

## Money and the queue — only in a transaction

- Charge + quota + inserting the job is one `BEGIN … COMMIT`.
- A later provider failure writes a compensating ledger entry, never
  `UPDATE balance`.
- Idempotency: `unique(provider, event_id)` for webhooks, a request hash for upstream
  calls, an idempotency key for the client's mutating requests.
- Statuses of jobs and payments are enums mapped with `#[sqlx(type_name)]`.

## Upstream resilience — always, not after the first outage

Every call to an external paid API has all five: a timeout; retry of idempotent calls
only, with backoff and jitter; a circuit breaker per host; a cache by request hash; the
raw response stored before parsing. Without a breaker one hung provider drains the
connection pool and the budget.

## Queue worker

- `select!` over the shutdown signal and Postgres `NOTIFY`; polling with jitter as the
  fallback; every job wrapped in `timeout`.
- Graceful shutdown: the server stops accepting, the worker finishes the current job or
  releases its lock before the container's deadline.

## Domain review questions

- Money, quotas and the job in one transaction; idempotency in place?
- Upstream: timeout, retry policy, circuit breaker, cache, raw response — all five?
- Every log line has a `request_id`; none has secrets or personal data?
- The endpoint is in the OpenAPI description; a public one is in the allowlist on purpose?
