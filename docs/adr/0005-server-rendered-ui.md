# 5. Render the UI on the server

Date: 2026-10-23 · Status: accepted

## Context

The interface is a handful of pages: a form, a plan to review, a progress page and a results table. A single-page app
would add a JavaScript build, an API contract to version and a second deployable.

## Decision

Pages are rendered with Jinja2 templates by FastAPI. A few lines of plain JavaScript poll the run status while it is
processing. No third-party scripts, which allows a strict Content-Security-Policy.

## Consequences

- One codebase, one deploy, fast pages, simple security story.
- Rich interactivity would need more work later; nothing planned needs it.
