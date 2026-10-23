.PHONY: install fmt lint typecheck test check run migrate migration

install:  ## Install the app and dev tools into .venv
	uv sync

fmt:  ## Format and auto-fix
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy app tests

test:  ## Needs Postgres for the db tests (see README)
	uv run pytest --cov

check: lint typecheck test  ## Everything CI runs

run:
	uv run uvicorn app.main:app --reload

migrate:
	uv run alembic upgrade head

migration:  ## make migration m="add runs table"
	uv run alembic revision --autogenerate -m "$(m)"
