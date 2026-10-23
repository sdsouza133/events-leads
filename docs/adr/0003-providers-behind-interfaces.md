# 3. Put every external service behind an interface with a fake

Date: 2026-10-23 · Status: accepted

## Context

The pipeline depends on five paid or rate-limited services: Claude, Apify, Apollo, Reoon and Resend. Calling them in
tests is slow, costs money, needs secrets in CI and gives different answers each time. Preview environments and the
QA suite need to run the whole pipeline end to end.

## Decision

Each service is a small Python protocol (for example `PostSearch.start(queries) -> run id`). There is a real
implementation that talks HTTP and a **fake** that returns deterministic, realistic data. `PROVIDERS=fake` selects the
fakes; production uses `PROVIDERS=real`. Response parsing is tested against fixture files shaped like real responses
(synthetic data only, never real people).

## Consequences

- Unit and integration tests run offline, fast and free.
- Preview environments run the full flow at no cost, so the QA agent can test every pull request.
- A real API change can still break production; the QA repo's nightly contract checks call the real APIs with tiny
  limits to catch that early.
