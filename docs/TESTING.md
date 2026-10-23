# Testing

## The layers

| Layer | Where | What it covers | Runs |
|---|---|---|---|
| Unit | `tests/` (no `db` marker) | Pure logic: domain matching, keyword building, attendance rules, ICP scoring, budget math, parsers | Every PR, in seconds |
| Integration | `tests/` (`@pytest.mark.db`) | Database, job queue, budget locking, the whole pipeline with fake providers, web routes | Every PR, against Postgres 16 |
| Contract | QA repo, nightly | Real APIs (tiny limits) still return what our parsers expect | Nightly |
| End-to-end | QA repo | A real browser through sign-in, approval and results on a preview or staging deploy | Every deploy |
| Smoke | QA repo | Key pages and one tiny run work | After each deploy and every 30 minutes on production |
| Load | QA repo | 500 concurrent users on the web tier; 1,000 queued runs | Before releases |
| Security | CI + QA repo | Dependency vulnerabilities, committed secrets, CodeQL, OWASP ZAP baseline scan | Every PR, nightly |

The independent QA lives in [event-leads-qa](https://github.com/sdsouza133/event-leads-qa): it only sees what a user
sees, so it can't share the app's blind spots.

## Running the tests

```bash
make test          # all tests; db tests are skipped if no Postgres is reachable
REQUIRE_DB=1 make test   # what CI does: fail instead of skip when the database is missing
```

The db tests use `DATABASE_URL` (default `postgresql+psycopg://el:el@127.0.0.1:5432/el_test`). Start one with Docker:

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_USER=el -e POSTGRES_PASSWORD=el -e POSTGRES_DB=el_test postgres:16
```

## Rules

- Tests never call real third-party APIs. Use the fakes in `app/providers/fakes.py`.
- Fixtures contain synthetic data only; never real names or emails.
- A bug fix starts with a failing test that reproduces it.
- Coverage must stay at or above 80% (CI fails below that).
