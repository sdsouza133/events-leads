# One image for every process. Railway runs it as the web service with the default command below; the background
# worker (arriving with the job queue) runs the same image with its own start command.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=build --chown=appuser:appuser /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=8000
USER appuser
EXPOSE 8000
# Behind Railway's edge proxy the client address arrives in X-Forwarded-For. The app is only reachable through that
# proxy, so trusting its forwarded headers is safe. uvicorn's access log is off: it records query strings, and a
# sign-in link carries its one-time token in one. The app logs each request itself, by path only.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
