# Security

## Reporting a problem

Please don't open a public issue. Report it privately through
[GitHub security advisories](https://github.com/sdsouza133/events-leads/security/advisories/new). We reply within
three working days.

## What we do

- Secrets live only in the hosting provider's environment variables; CI scans every change for committed secrets.
- Dependencies are scanned for known vulnerabilities on every pull request and updated weekly by Dependabot.
- CodeQL static analysis runs on every pull request and weekly.
- Personal data (names, emails, LinkedIn URLs) is shown only to the account that requested it and to operators, is
  never logged, and is deleted after 30 days.
- See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security) for sign-in, authorization, CSRF and SSRF protections.
