"""The web app: `uvicorn app.main:app`."""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.db import database_is_reachable
from app.logging_config import configure_logging

log = logging.getLogger("app.http")

# Applied to every response. The UI is server-rendered with no third-party scripts, so the policy can be strict.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
HSTS = "max-age=31536000; includeSubDomains"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="events-leads",
        version=__version__,
        # The interactive docs expose every route; keep them out of production.
        docs_url="/docs" if settings.env in ("dev", "test") else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env in ("dev", "test") else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers.update(SECURITY_HEADERS)
        if settings.base_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = HSTS
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Never touches the database, so a database outage doesn't restart us."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness: we can serve traffic, which needs the database."""
        if database_is_reachable():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "database unavailable"}, status_code=503)

    return app


app = create_app()
