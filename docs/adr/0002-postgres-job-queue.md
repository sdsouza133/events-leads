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

## Consequences

- One stateful service (Postgres) instead of two; job state and data can't disagree.
- Throughput is in the thousands of jobs per minute, far above what the external APIs allow us anyway.
- If we ever need much more, the queue sits behind a small interface (`enqueue`, `claim`, `complete`, `fail`) and
  can be swapped.
