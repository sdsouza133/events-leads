# 7. Let operators answer Claude's questions until there is an API key

Date: 2026-10-24 · Status: accepted

## Context

The service is ready to run before its Claude API key is. Three steps need Claude: reading the company's website,
finding events, and labeling posts. Without the key, every run would stop at the first of them.

## Decision

With real providers and no `ANTHROPIC_API_KEY`, a stand-in (`ManualLlm`) turns each question into a task in the
database, and the job that asked waits for an answer without using up its attempts. Operators answer through an
authenticated JSON API (`/admin/api/tasks`, with `OPERATOR_API_TOKEN` as a bearer token). An answer is checked
against the same schemas Claude's answers are held to before it is stored, so the rest of the pipeline can't tell
the two apart. Setting the key switches Claude back on with no other change.

## Consequences

- Runs work end to end without the key, at no Claude cost, but only as fast as an operator answers. Labeling posts
  is the slow part: each batch of 25 posts is a task of its own, asked one after another.
- The operator API is a second way in. It sits behind a long random token compared in constant time, and it isn't
  there at all while the token is unset.
- While a website question waits, its job reads the site again each time it checks back (every 2 minutes), because
  the analyze step reads the site before asking.
- Each check back also reserves the call's estimated cost and releases it, so the `spend` table gets a released row
  each time. The day's and the run's totals are unaffected.
