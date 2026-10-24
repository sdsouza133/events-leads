# Security

## Reporting a problem

Please don't open a public issue. Report it privately through
[GitHub security advisories](https://github.com/sdsouza133/events-leads/security/advisories/new). We reply within
three working days.

## What we do

- Secrets live only in the hosting provider's environment variables; CI scans every change for committed secrets.
- Dependencies are scanned for known vulnerabilities on every pull request and updated weekly by Dependabot.
- CodeQL static analysis runs on every pull request and weekly.
- Personal data (names, emails, LinkedIn URLs) is shown only to the account that requested it and to operators, and
  is deleted after 30 days by a job that runs every hour: a list when its results expire, 30 days after it was
  ready, and the posts and people behind it once no list holds them, unless a post was fetched or a person looked
  up in Apollo in the last 30 days. Sign-up counters, which hold IP addresses, go after a day. The
  [runbook](docs/RUNBOOK.md#personal-data-after-30-days) lists everything the job deletes.
- Logs identify people by internal IDs and never contain lead data or sign-in links; a requester's email appears
  only masked, on the sign-in and rate-limit lines written before an account exists.
- See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security) for sign-in, authorization, CSRF and SSRF protections.
