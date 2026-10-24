# 2. Use Postgres as the job queue

Date: 2026-10-23 · Status: accepted

## Context

The pipeline runs in the background and must survive crashes and redeploys. The usual choice is a broker such as
Redis or RabbitMQ with Celery or RQ. That is one more service to run, secure, monitor and pay for, and it splits state
between two systems: a job can say "done" while the database write it depended on was rolled back.

## Decision

Jobs are rows in a `jobs` table. Workers claim one with `SELECT ... FOR UPDATE SKIP LOCKED`, so several workers never
take the same job. A job's own database writes and its completion commit in the same transaction. Failed jobs retry
with exponential backoff; jobs whose worker died are released after a timeout.

### Side effects outside the database

Postgres can roll back our writes but not a call to another service. A worker can crash after a paid call or an
email and before its job commits, so the job runs again. Every handler that calls out must therefore make the retry
safe, using these rules, and have a test that runs it twice:

1. **Record the intent first, in its own commit.** Before starting the call, write what is about to happen (the spend
   reservation, the search being started) and commit. Right before the call, mark the reservation as started (one
   more short commit). After the call, store the provider's handle right away (for example the Apify run ID on the
   run's event row).
2. **On retry, look before acting.**
   - Reservation never marked started: the call never happened. Release it and start over.
   - Handle stored: resume it (poll that Apify run) instead of starting a new one, and settle the reservation from
     the provider's own record of what it charged.
   - Started, but no handle (the worker stopped during the call): settle at the reserved amount, which is the most
     the call could cost (an Apify run is capped at it by `maxTotalChargeUsd`; a Claude call is bounded by its
     prompt, `max_tokens` and `max_uses`), then start over. An unknown outcome counts as spent, never as freed.
   - A sweeper applies the same rules to any reservation still open an hour after it was made, so a run that died
     can't hold budget.
3. **Cache results by their natural key.** Model answers by post and event, Apollo results by LinkedIn URL. A retry
   re-reads the cache, so at most one in-flight batch is ever paid for twice, and that batch's cost was reserved.
4. **Send email through an outbox with an idempotency key.** Each email gets one row with a unique key (for example
   `results:<run id>`), and the key goes to Resend as its `Idempotency-Key`, so a resend after a crash is dropped by
   Resend instead of reaching the requester twice.

## Consequences

- One stateful service (Postgres) instead of two; job state and data can't disagree.
- Throughput is in the thousands of jobs per minute, far above what the external APIs allow us anyway.
- If we ever need much more, the queue sits behind a small interface (`enqueue`, `claim`, `complete`, `fail`) and
  can be swapped.
