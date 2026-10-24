# Product requirements: events-leads

Status: draft v0.1 · Owner: Sunny D'Souza · Last updated: 2026-10-23

## Problem

B2B teams know that events are where their buyers gather, but finding *which* buyers are going to a given event is
slow manual work: scrolling LinkedIn for "see you at…" posts, checking each person against the ideal customer profile
(ICP), then hunting for a working email. Most teams skip it, or pay for an event-data product they use twice a year.

## The product

A free, self-serve tool a company can use on its own. A company enters its website; the tool works out its ICP and the
events its buyers go to, and returns a list of people who are attending (or just attended) those events, who match the
ICP, with a LinkedIn URL and a verified email for each.

It is a lead magnet for B2B Dynamics: no sales pitch inside the product. The value is the list itself.

## Users

| User | What they want |
|---|---|
| **Requester**: a founder, marketer or sales lead at a B2B company | A short, accurate list of ICP people going to the events that matter to them, without doing research |
| **Operator**: Sunny | Control over spend, visibility into who used it, and the ability to grant extra runs |

## The journey

1. The requester enters their **website** and their **work email** (on the same domain as the website).
2. They click the sign-in link we email them. Nothing paid happens before this.
3. We read their website and propose:
   - their **ICP**: industries, company sizes, regions, job titles and seniority;
   - **events** in their space from the last 60 days or the next 90 days, with dates and links, found with live web
     search so they are real;
   - the **search keywords** we'll use for each event.
4. They edit any of it, pick up to 3 events (they can add their own), and approve.
5. We search LinkedIn posts for those events, keep the people who are actually attending (not organizers, not people
   promoting tickets), keep the ones who match the ICP, and find a verified email for each.
6. They get an email with a link to their results: a table and a CSV with name, title, company, LinkedIn URL, email,
   the event, and the post that shows they're going.

## Functional requirements

- **FR-1 Sign-in.** Passwordless sign-in by a one-time link sent to the email. Links expire after 30 minutes and work
  once. Free-mail addresses (gmail.com and similar) are refused. The email's domain must match the website's.
- **FR-2 Site analysis.** Read up to 5 pages of the website (home plus pages like About, Customers, Pricing) and produce
  an ICP and keyword suggestions. Must handle sites that block us or are mostly JavaScript by saying so, not guessing.
- **FR-3 Event discovery.** Suggest 3–8 events with name, dates, city or "online", URL and a one-line reason. Every event
  must come from a web search result, not from the model's memory.
- **FR-4 Approval.** The requester can edit the ICP, choose up to 3 events, add their own (name, date, URL), and edit
  keywords before anything is collected.
- **FR-5 Collection.** Search public LinkedIn posts for each approved event. No logged-in scraping, no cookies.
- **FR-6 Qualification.** For each post, decide: attending / attended / speaking / exhibiting / organizer /
  promoting / not attending / unclear. Only the first four continue. Then check each person against the ICP.
- **FR-7 Enrichment.** Look up each qualified person in Apollo by LinkedIn URL. Keep only emails Apollo marks
  verified. Optional second check with Reoon (off by default) drops only hard-invalid addresses.
- **FR-8 Delivery.** Results page and CSV, available to the requester for 30 days, then deleted.
- **FR-9 Operator console.** Runs, their status and cost, today's spend, queue health, and buttons to cancel a run or
  grant a company another run.

## Limits (configurable)

| Limit | Default |
|---|---|
| Free runs per company (website domain) | 1 |
| Events per run | 3 |
| Leads delivered per run | 50 |
| LinkedIn searches per event | 4 |
| Posts per search | 50 |
| Spend per run, dollar-billed services (Apify + Claude) | $3 |
| Of that, the analysis before approval (Claude only) | $0.50 |
| Spend per day, dollar-billed services, all runs together | $20 (new runs wait in line when it's reached) |
| Apollo lookups per run (credit-billed) | 150 |
| Reoon checks per run, when enabled (credit-billed) | one per delivered lead, at most 50 |
| Sign-up attempts per IP address per hour | 5 |

## Non-functional requirements

- **Cost:** a typical run costs under $3 in Apify and Claude fees. Posts and their attendance labels are cached per
  event and reused across companies, so popular events get cheaper.
- **Scale:** 10,000 registered companies; 500 runs a day; web pages respond in under 300 ms at the 95th percentile.
- **Reliability:** a crash or redeploy never loses a run; work resumes where it stopped.
- **Security and privacy:** OWASP Top 10 covered; results visible only to the requester and the operator; personal data
  deleted after 30 days. Logs never contain lead data or sign-in links; a requester's email appears only masked, on the
  sign-in and rate-limit lines written before an account exists.
- **Accessibility:** WCAG 2.1 AA for the requester-facing pages.

## Success metrics

- Signed-in requesters who approve a plan: > 60%
- Runs that deliver at least 20 leads: > 70%
- Requesters who download the CSV: > 50%
- Median cost per completed run: < $2

## Out of scope for v1

Paid plans, team accounts, CRM export, scheduled re-runs, and attendee lists from sources other than LinkedIn posts
(event apps, sponsor pages).

## Open questions

- Should exhibitors (vendors at a booth) count as leads? Default: yes, labeled "exhibiting", and the ICP check decides.
- Which model handles bulk post classification? Default: Claude Opus 5.5 at low effort; Claude Haiku 5.5 would cut that
  cost by roughly 40 times. The operator decides (`CLASSIFY_MODEL`).
