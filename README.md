# events-leads

Find the people going to your industry's events, filtered to your ICP, with verified emails and LinkedIn URLs.

A company enters its website. We work out its ideal customer profile and the events its buyers attend, search public
LinkedIn posts for people who are going (or just went), keep the ones who match the ICP, and return each person's
LinkedIn URL and an Apollo-verified email. Free, self-serve, capped per company.

> Status: **in development**. Built in the open, one reviewed pull request at a time.

## How it works

1. **Sign in** with a work email on the same domain as the website (one-time link, no password).
2. **Review the plan:** the ICP, 3–8 real events from live web search, and the search keywords. Edit anything, pick up
   to 3 events or add your own, then approve.
3. **We collect and qualify:** LinkedIn post search (Apify), attendance and ICP checks (rules first, then Claude),
   verified emails (Apollo).
4. **Get the list** by email: name, title, company, LinkedIn, email, event, and the post that shows they're going.

Details: [product requirements](docs/PRD.md) · [architecture](docs/ARCHITECTURE.md) ·
[decisions](docs/adr/) · [testing](docs/TESTING.md) · [runbook](docs/RUNBOOK.md)

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 + Alembic · PostgreSQL 16 (Supabase in production) · a Postgres-backed job queue ·
Jinja2 pages · Docker on Railway · GitHub Actions, CodeQL, CodeRabbit.

## Local development

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and Postgres 16 (or Docker).

```bash
uv sync
cp .env.example .env          # set SECRET_KEY
make migrate
make run                      # http://localhost:8000
make check                    # lint, type check and tests, the same as CI
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and [SECURITY.md](SECURITY.md) to report a problem.
