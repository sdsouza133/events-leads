"""The web app: `uvicorn app.main:app`."""

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app import __version__
from app.config import get_settings
from app.db import database_is_reachable
from app.logging_config import configure_logging

log = logging.getLogger("app.http")

# Applied to every response, errors included. The UI is server-rendered with no third-party scripts, so the policy
# can be strict.
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

# A caller's X-Request-ID is reused only if it's a short plain token; anything else gets a fresh one, so clients
# can't bloat or pollute the logs.
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def request_id_for(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    # No interactive API docs: the product is server-rendered pages, not a public API, and the docs page would need
    # a looser Content-Security-Policy.
    app = FastAPI(title="events-leads", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request_id_for(request)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Handled here rather than by Starlette's outer error handler, so a failed request still gets the
            # security headers, its request ID and a log line that ties the traceback to it.
            log.exception("unhandled error", extra={"request_id": request_id, "path": request.url.path})
            response = PlainTextResponse("Something went wrong on our side.", status_code=500)
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
