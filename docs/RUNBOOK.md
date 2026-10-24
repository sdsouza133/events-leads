# Runbook

What to do when operating the service. Keep it current: every incident should leave this page better.

## Deploying

- Merging to `main` deploys to **staging** automatically. The QA smoke and end-to-end suites run against it.
- Production is promoted from a staging build that passed QA (Railway: the staging deployment → *Promote*, or merge the
  release PR when release automation is added).
- Database migrations run before the new version starts (`alembic upgrade head` as Railway's pre-deploy command). A
  migration that fails stops the deploy and the old version keeps running.

## Rolling back

1. Railway → the service → Deployments → the last good deployment → **Redeploy**.
2. If the bad version ran a migration, check that migration's `downgrade()` is safe before running
   `alembic downgrade -1`. Prefer a forward fix when data was written in the new shape.

## The worker

- Start: `python -m app.worker` (locally: `make worker`). Same image as the web service, different start command.
- SIGTERM (a redeploy) lets the current job finish, then exits. A job cut off by a hard kill is put back after
  `WORKER_STALE_AFTER_SECONDS` (5 minutes) and its open spend reservation is closed by the rules in ADR 0002.
- Logs: every job writes `job started` and `job ended` (with `job_id`, `kind`, `run_id`, `ms`); failures write
  `job failed` with the error and whether it will be retried. `housekeeping` lines report jobs put back and
  reservations closed.
- A run stuck in `analyzing`: look up its jobs by `run_id`. A job `queued` with a future `run_after` is waiting
  (retry backoff, or the daily cap: it resumes after midnight UTC). A job `failed` should have failed its run too.
- A run stuck in `collecting`: look at its `run_events`. `searching` with an `apify_run_id` is polled every
  `SEARCH_POLL_SECONDS`; the run is visible in the Apify console under that id. After 30 minutes the search is
  given up and tried once more. `pending` with no `apify_run_id` is either waiting for another run's search of the
  same event or, if the run is `waiting_for_budget`, for the next UTC day. `error` says why an event failed.
- A run stuck in `qualifying`: its `qualify` job defers to itself between turns, so a queued job with a recent
  `run_after` is working. `status_detail` says when it waits for the next day's budget. `post_labels` shows what
  is labeled (`method` rule, model, or skipped when Claude refused the post); `error` on the run notes when its
  budget ran out mid-way. A run failed in `qualifying` with nothing labeled usually means Claude rejected the
  request itself (the job's `last_error` names the status): check `ANTHROPIC_API_KEY` and `CLASSIFY_MODEL`.
- A run stuck in `enriching`: its `enrich` job defers to itself between turns. `apollo_credits_used` on the run
  grows by one per person Apollo found, up to `APOLLO_CREDITS_PER_RUN`. `run_leads.exclude_reason` says why each
  person was left off the list. A run failed in `enriching` usually means Apollo rejected the request (check
  `APOLLO_API_KEY` and the account's credits). A run `ready` whose `error` says "results email not sent" has its
  results; the requester can sign in to see them.

## A sign-in email that never arrived

- Look up the account by email and its `send_sign_in` jobs (payload `account_id`). A job `failed` names Resend's
  answer in `last_error`; `done` means Resend accepted it, so check Resend's dashboard for a bounce or a spam block.
- The requester can ask for a new link at `/sign-in-again`, which every "link expired" and "sign in" page points to
  (5 times an hour per address, counted with the start form). Links last 30 minutes and work once.
- Previews keep emails instead of sending them: operators read them on `/dev/outbox`.

## Pausing spend immediately

Set `PAUSE_NEW_RUNS=true` on the worker service. Runs already collecting finish their current step and stop; nothing new
starts. To also stop runs in progress, scale the worker to zero.

## Rotating a secret

1. Create the new key at the provider.
2. Update the variable in Railway for the affected environment; Railway redeploys.
3. Revoke the old key at the provider.

## When something is wrong

1. **Look:** `/readyz`, Railway logs (filter by `request_id`, `run_id` or `job_id`), Sentry, the operator console.
2. **Contain:** pause spend (above) if money or data is at risk.
3. **Fix or roll back.**
4. **Write it up** within a week: what happened, impact, timeline, root cause, what we changed so it can't recur.
   Blameless: the question is which safeguard was missing, not who made the mistake.
