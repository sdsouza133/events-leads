# Architecture

## The big picture

```mermaid
flowchart LR
    U[Requester's browser] -->|HTTPS| W[Web app<br/>FastAPI]
    O[Operator] -->|HTTPS| W
    W <--> DB[(Postgres<br/>Supabase)]
    K[Worker<br/>background jobs] <--> DB
    K --> A[Claude API<br/>ICP, events, attendance]
    K --> P[Apify<br/>LinkedIn post search]
    K --> AP[Apollo<br/>people + verified emails]
    K -.optional.-> R[Reoon<br/>email check]
    K --> M[Resend<br/>sign-in and results emails]
```

Two processes from one codebase and one Docker image:

- **Web** serves pages and the operator console. It never calls an outside service, not even to send email: it writes
  jobs (and outbox emails) to the database, and the worker does the rest. A slow or failing provider therefore can't
  slow down or break a page.
- **Worker** runs the pipeline as a sequence of jobs. Several workers can run at once.

Postgres is the only stateful piece. It holds the data, the job queue, the spend ledger and the rate-limit counters,
so there is no Redis to run (see [ADR 0002](adr/0002-postgres-job-queue.md)).

## The pipeline

Each run moves through these states. Every arrow is one background job, and every job can safely run twice.

```mermaid
stateDiagram-v2
    [*] --> awaiting_sign_in: website + work email
    awaiting_sign_in --> analyzing: sign-in link clicked
    analyzing --> awaiting_approval: ICP, events, keywords proposed
    awaiting_approval --> collecting: requester approves
    collecting --> qualifying: posts fetched for every event
    qualifying --> enriching: attendees and ICP matches picked
    enriching --> ready: verified emails found
    ready --> expired: after 30 days
    analyzing --> failed
    collecting --> failed
    qualifying --> failed
    enriching --> failed
    collecting --> waiting_for_budget: daily cap reached
    waiting_for_budget --> collecting: next day
```

1. **analyze**, two jobs. `analyze_site` reads the home page and up to four pages about the customers (see Security)
   and asks Claude for the ICP; a site that can't be read, or has too little text, fails the run with the reason
   instead of a guess. `find_events` asks Claude, with live web search, for real events, and builds up to four
   LinkedIn searches per event. Results are stored on the run for the requester to edit. Both jobs stay within the
   analysis budget, the only spend before approval.
   - **The plan** (`app/web/plan.py`): while the run awaits approval, its page shows who we'll look for as chips and
     the events as cards, the first three picked, so a plan that looks right takes one press of "Find my leads".
     Behind a click each: "Adjust" edits the ICP, a card's "Searches" its LinkedIn searches, "Show more" the other
     events, and "Add an event you know" up to three events of the requester's own, whose searches are built the
     same way from the name and date. Edits are saved on the run as they go; approving hands the picked events to
     `collect.approve`, which starts collecting. The form works without JavaScript; `plan.js` only keeps the count
     live and says to unpick one before a fourth. While a run works, its page ticks off a few steps
     (`run_page.progress`), which `run.js` updates from the status endpoint.
2. **collect**, one `collect_event` job per approved event (at most three). A search any run made for the same event
   in the last `SEARCH_CACHE_DAYS` (3) is reused, and a run waits for another run's search of the event rather than
   pay for the same posts; the rest go into one Apify run capped by `maxTotalChargeUsd`. The job stores the Apify
   run's id before polling it, so a worker that stops mid-search picks the same run back up. Posts, which search
   found each, and the searches themselves go into the shared cache. A search that fails or runs past 30 minutes is
   tried once more, then only its event is dropped; the run fails only if no event could be searched.
3. **qualify**, one `qualify` job per run. Text rules label the obvious posts for free (`app/pipeline/rules.py`);
   Claude labels the rest, 25 posts per call, a few calls per turn of the job, which then defers to itself so other
   runs get a turn. Labels are cached per post and event, so a run reuses what earlier runs paid for. Company pages
   are never labeled. Each person who was there (attending, attended, speaking, exhibiting) becomes one candidate
   with their most telling post; anyone whose headline already rules them out (an excluded title, a student, the
   requester's own colleague) is kept with the reason and skipped by the paid steps.
4. **enrich**, one `enrich` job per run. Apollo bulk match by LinkedIn URL, 10 people per call, candidates whose
   headline fits a persona first. Whatever Apollo answers, found or not, is cached on the person for
   `PERSON_CACHE_DAYS` (30 days, as long as personal data may be kept), so other runs reuse it. When two runs ask
   about the same person at once, the answer to the later question is the one kept. The ICP score
   (`app/pipeline/scoring.py`) uses the enriched title, seniority, company industry and company size, and its
   reasons are kept on the lead. People without an email Apollo marks verified, with an excluded title, at the
   requester's company, or scoring below `MIN_ICP_SCORE` (40) are left out with the reason; the best 50 make the
   list. With Reoon on, those 50 addresses get a second check that removes only invalid, disabled and spam-trap
   addresses. Like qualify, the job makes a few calls per turn, then defers to itself.
5. **deliver**: the run is marked ready (results kept for 30 days) and a `deliver` job emails the requester the
   link, with `results:<run id>` as Resend's idempotency key. A send that would come more than 23 hours after the
   run was ready is dropped instead, since Resend only remembers a key for 24.
   - **The list** (`app/web/results.py`): the run's page shows the people included as cards, best fit first (ICP
     score, then speakers and exhibitors before attendees), filterable by how they're at the event, with a CSV of
     the same rows. While the search runs, its page counts the posts found, the people seen at the events and the
     verified emails so far (`run_page.found_so_far`), with a bar for how far along it is. Spreadsheet formulas can't ride
     along in the CSV: a cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
     apostrophe. Links to posts and profiles are shown only if they're web addresses. Once `results_expire_at`
     has passed, the page says the list has expired and the CSV answers 410 Gone.

### How the worker runs jobs

- One job at a time per worker process; add processes for throughput. Jobs are claimed with `SKIP LOCKED`.
- A handler reads what it needs and commits **before** any slow outside call, then writes its results; the worker
  commits those together with the job's completion. No transaction stays open while a provider answers (production
  enforces this: Postgres ends a session idle in a transaction for 60 seconds).
- Paid calls follow ADR 0002: reserve, mark started, call, then settle or release by what the failure (or answer)
  says it cost. A retried step first closes what its earlier attempt left open.
- A heartbeat thread keeps a long job's claim fresh. Every minute the worker puts back jobs whose worker went silent
  (5 minutes by default; on the last attempt the job gives up, which fails its run), closes spend reservations
  left open for over an hour, and queues the retention job if no copy is waiting.
- A worker that lost its claim to another (it was too slow) drops its writes; the new owner redoes the work.
- `PAUSE_NEW_RUNS` stops spending without stopping the worker: every call to Claude, Apify, Apollo or Reoon first
  checks it, and a job that would make one is put back for 5 minutes, without using up an attempt. Polling an Apify
  search already running goes on, so its posts aren't lost. A worker reads its settings when it starts, and changing
  the variable restarts it, so a worker told to stop starts no paid call either: its job goes straight back for the
  next worker.

### Manual AI mode

With real providers and no `ANTHROPIC_API_KEY`, `ManualLlm` (`app/providers/manual.py`) stands in for Claude
([ADR 0007](adr/0007-manual-ai-mode.md)). Each
question becomes an `ai_tasks` row, found again by what identifies it (the website, the analysis, the posts), and
the job that asked is put back for 2 minutes without using up an attempt. Its spend reservation is released, since
nobody billable was asked. Operators answer through the operator API (`/admin/api/tasks`, with `OPERATOR_API_TOKEN`
as a bearer token). An answer must fit the schema Claude's answer would have to, and its question: events within the
window asked about, one label for each post. The job then carries on as if Claude had answered, at no cost. Each
run asks its own events question, and the retention job deletes questions after 30 days, answered or not, since
they can quote people's posts.

### Deleting personal data

The `retention` job (`app/pipeline/retention.py`, issue #18) runs every hour and deletes by age, so a second round
right after the first finds nothing. After each round it defers itself an hour, which uses up no attempt; its dedupe
key lets one copy at most wait or run, and an advisory lock keeps any two rounds from overlapping.

- **Lists:** a `ready` run whose `results_expire_at` has passed becomes `expired` and its `run_leads` are deleted. A
  run that failed or was canceled loses its leads 30 days after it stopped.
- **Shared data:** once no lead points at them, `posts` fetched more than 30 days ago go (a post's `event_posts`
  and `post_labels` with it), and so do `people`, unless Apollo or the email check looked them up in the last 30
  days: that answer is reused. Someone never looked up holds nothing worth keeping, so they go as soon as no list
  holds them. `post_labels`, `search_cache` and `ai_tasks` older than 30 days go regardless.
- **Everything else holding personal data or growing without end:** `outbox_emails` after 30 days, `rate_limits`
  after a day, expired `login_tokens` and `sessions`, and `jobs` finished more than 30 days ago.

A lead's person and post can't be deleted while the lead exists: `run_leads`' foreign keys to `people` and `posts`
are `RESTRICT`. A list that is still shown keeps everyone on it, even when a run adds someone at the very moment
their data is deleted: the round fails on the foreign key, nothing is deleted, and the retry leaves them be.

### The operator console

`/admin` (`app/web/console.py`), for the addresses in `ADMIN_EMAILS` only; to anyone else signed in it doesn't exist.
It lists the latest runs with their status, spend and internal error, today's spend against the daily cap, and the
queue with the latest failed jobs. Canceling a run sets it to `canceled`, which every step checks before starting
anything. Each grant (`run_grants`) lets a company start one more run on top of `FREE_RUNS_PER_DOMAIN`. Operators
sign in at `/sign-in-again`, and their link opens the console.

## Data model (main tables)

| Table | Holds |
|---|---|
| `accounts`, `login_tokens`, `sessions` | Requesters and operators; one-time sign-in tokens and sessions, stored hashed |
| `runs` | One per company request: website, status, ICP, proposed and approved events, cost so far |
| `events`, `run_events` | Canonical events, and which events (and keywords) a run uses |
| `posts`, `event_posts`, `search_cache` | The shared post cache and which searches already ran |
| `post_labels` | Attendance label per post and event, with the method (rule or model) |
| `people` | One row per LinkedIn profile with cached Apollo data |
| `run_leads` | The final list for a run, with ICP score and reasons; its people and posts stay while it does |
| `jobs` | The job queue |
| `spend`, `budget_days` | Every paid call, and the running total per day for the daily cap |
| `rate_limits` | Fixed-window counters for sign-up and sign-in attempts |
| `outbox_emails` | Emails kept instead of sent where there's no Resend key: local work and previews |
| `run_grants` | Extra runs an operator gave a company, one row per run |
| `ai_tasks` | Manual AI mode: questions for Claude that operators answer instead |

## Cost controls

- Nothing runs before the requester signs in. The analyze step then runs before approval, because it produces the
  plan the requester approves. It calls only Claude, has its own cap ($0.50, part of the run's $3) and counts toward
  the daily cap. Apify, Apollo and Reoon run only after the requester approves.
- Dollar-billed calls (Apify, Claude) first **reserve** their estimated cost against the run's cap and the day's cap in one database
  transaction (row lock on today's budget), so two workers can't both squeeze under the limit. Actual cost is
  settled afterwards.
- Apify runs carry `maxTotalChargeUsd`, so even a runaway search can't exceed its reservation.
- Credit-billed calls have their own per-run limits: Apollo credits (150 per run, one per person found; a batch
  never asks about more people than the credits left) and, when enabled, Reoon checks (one per place on the list,
  at most 50). Resend's free tier covers our email volume; sign-ins are rate-limited per IP and email.
- Caches mean popular events and people are paid for once.

## Scaling to 10,000 users

The web tier is stateless; add replicas. The real limits are external:

| Constraint | At 500 runs/day | Plan |
|---|---|---|
| Apify spend | ≈ 300,000 posts/day before caching | Event cache, query caps, daily budget |
| Apollo | ≈ 75,000 lookups/day before caching | Person cache, ICP pre-screen before lookup |
| Claude | ≈ 15,000 classification calls/day | Rules first, batches of 25 posts, label cache |
| Postgres connections | Supabase pooler limits | Small pools per process (5 + 5 overflow) |
| Database size | ≈ 1 MB per run | 30-day retention for results and raw posts; Supabase Pro at scale |

A load test in the QA repo checks the web tier at 500 concurrent users and the queue with 1,000 runs (fake providers).

## Security

- **Sign-in:** one-time links, hashed in the database, 30-minute expiry, single use. The worker makes the token when
  it sends the email, so it never passes through the web process, the job queue or a log (uvicorn's access log, which
  would record it, is off). The link opens a page whose button signs in, so mail scanners that open links can't use
  them up. Sessions are random tokens in an HttpOnly, Secure, SameSite=Lax cookie, stored hashed and revocable.
  Someone who has asked for a run before gets a new link from `/sign-in-again` (their link expired or was opened on
  another device, or they signed out). It signs them in to their latest run, never starts one, and doesn't say
  whether an address has an account. An operator's link opens the console instead, and an operator needn't have
  asked for a run first: an address listed in `ADMIN_EMAILS` gets its account made on the spot.
- **Sign-up abuse:** the start and sign-in-again forms together take 5 attempts an hour per IP address and per email
  address (kept as a hash), and a company's free run is taken under a lock on its domain, so two colleagues signing
  in at once can't both use it.
- **Authorization:** a run and its leads are visible only to the account that created it and to operators
  (`ADMIN_EMAILS`). Anyone else gets "not found", so run ids can't be probed.
- **CSRF:** every form carries a token that must match a cookie only our pages set (double submit), so a form
  posted from another site is refused.
- **SSRF:** the website fetcher resolves the host itself, refuses private, loopback, link-local and reserved
  addresses, connects to the address it checked (no DNS rebinding), re-checks every redirect, and caps size and time.
- **Headers:** strict Content-Security-Policy, HSTS, no framing. The policy allows only the site itself, so pages
  use no inline styles or scripts, and the fonts (Inter and Bricolage Grotesque, SIL Open Font License, in
  `app/static/fonts`) and icons (`app/web/icons.py`) are served from the site. `tests/test_web_assets.py` checks it.
- **Request size:** a body over 256 KB is refused with 413 before it is read into memory, whether its
  Content-Length says so or it arrives chunked (`LimitBody` in `app/main.py`). The largest real ones, the plan form
  and an operator's answer about 25 posts, are around ten kilobytes.
- **Secrets:** only in environment variables (Railway); never in the repo or logs.
- **Logs:** identify people by internal IDs (account, run, person). Lead data (names, emails, LinkedIn URLs),
  requesters' email addresses and sign-in links are never logged. A sign-up attempt over the limit is logged without
  who made it; the `rate_limits` table shows which IP address or address hash went over.
- **Data retention:** personal data in results and caches is deleted after 30 days by the hourly retention job
  (see [Deleting personal data](#deleting-personal-data)).

## Environments

| Environment | Where | Providers | Database |
|---|---|---|---|
| Local | your machine | fakes | local Postgres |
| Preview | Railway, one per pull request | fakes | Railway Postgres (throwaway) |
| Staging | Railway `staging` | real, tiny caps | separate database |
| Production | Railway `production`, `events.theb2bdynamics.com` | real | Supabase |

Fakes are deterministic stand-ins for every provider, so previews and the QA suite can run the whole pipeline end to
end without spending money ([ADR 0003](adr/0003-providers-behind-interfaces.md)).

## Observability

JSON logs with request, run and job IDs; error tracking in Sentry; `/healthz` (process up) and `/readyz` (database
reachable) for Railway's health checks; the operator console for run and spend status.
