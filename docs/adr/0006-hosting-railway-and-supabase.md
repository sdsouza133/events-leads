# 6. Host on Railway, store production data in Supabase

Date: 2026-10-23 · Status: accepted

## Context

We need hosting for two long-running processes (web and worker), per-pull-request preview environments, and a
managed Postgres with backups.

## Decision

- **Railway** runs the web and worker services from this repository's Docker image, with `staging` and `production`
  environments and automatic preview environments for pull requests.
- **Supabase** hosts the production Postgres (daily backups, point-in-time recovery on paid plans), reached through
  its Session pooler.
- Previews and staging use Railway's own Postgres, created and thrown away with the environment.

## Consequences

- Production data lives with a dedicated database provider, separate from the app host.
- Preview databases cost nothing to throw away.
- Production and previews use the same Postgres version (16) so behavior matches.
