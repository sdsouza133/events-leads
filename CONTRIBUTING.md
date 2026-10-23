# Contributing

## How work flows

1. **Issue first.** Every change starts as a GitHub issue: a user story with acceptance criteria (feature) or steps to
   reproduce (bug). It goes on the project board: Backlog → Ready → In progress → In review → Done.
2. **Branch.** One short-lived branch per issue, named `feat/…`, `fix/…`, `chore/…`, `docs/…` or `test/…`.
3. **Commit** with [Conventional Commits](https://www.conventionalcommits.org/): `feat: add attendance rules`,
   `fix: stop double-counting Apify spend`. The type says what kind of change it is; the changelog is built from them.
4. **Open a pull request** using the template and link the issue (`Closes #12`). Keep PRs small enough to review in
   15 minutes; split anything bigger.
5. **Checks must pass:** lint, types, tests (with Postgres), dependency and secret scans, CodeQL, the Docker build.
6. **Review:** CodeRabbit reviews automatically; the code owner reviews and merges. Every comment gets either a fix or a
   reply saying why not.
7. **Squash and merge.** `main` is always deployable; merging deploys to staging.

## Definition of done

- Acceptance criteria met and covered by tests.
- `make check` passes.
- Docs updated where behavior changed; an ADR for any new architectural decision.
- No secrets or personal data anywhere in the diff.

## Local setup

```bash
uv sync                      # installs the app and dev tools into .venv
cp .env.example .env         # then fill in SECRET_KEY
uv run pre-commit install    # format and lint on every commit
make migrate && make run     # http://localhost:8000
```

## Changing the database

Edit `app/models.py`, then `make migration m="describe the change"`, read the generated file, and commit both. CI
fails if the models and migrations disagree.
