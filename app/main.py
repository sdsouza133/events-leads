"""The web app: `uvicorn app.main:app`."""

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__, web
from app.config import get_settings
from app.db import database_is_reachable
from app.logging_config import configure_logging
from app.web import common

STATIC = Path(__file__).resolve().parent / "static"

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

# The largest body any page or the operator API takes. The biggest real ones, the plan form and an operator's answer
# about 25 posts, are around ten kilobytes. Starlette reads a whole body into memory, so a bigger one is refused first.
MAX_BODY_BYTES = 256 * 1024
TOO_LARGE = "That request is too large."


class LimitBody:
    """Refuses a request body over `max_bytes` with 413: up front when its Content-Length says so, and while it
    arrives otherwise (a chunked body declares no length)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await PlainTextResponse(TOO_LARGE, status_code=413)(scope, receive, send)
            return
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:  # FastAPI answers this where the body is being read
                    raise HTTPException(status_code=413, detail=TOO_LARGE)
            return message

        await self.app(scope, counted, send)


def wants_a_page(request: Request) -> bool:
    """A browser opening an address, as opposed to a script or a stylesheet, font or image it loads."""
    return request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", "")


def request_id_for(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    # No interactive API docs: the product is server-rendered pages, not a public API, and the docs page would need
    # a looser Content-Security-Policy.
    app = FastAPI(title="events-leads", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LimitBody, max_bytes=MAX_BODY_BYTES)  # inside request_context, which adds the headers

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

    @app.exception_handler(StarletteHTTPException)
    async def not_found(request: Request, exc: StarletteHTTPException) -> Response:
        """A browser opening an address no page lives at gets the site's own page, not the framework's JSON."""
        if exc.status_code == HTTPStatus.NOT_FOUND and wants_a_page(request):
            return await run_in_threadpool(common.missing_page, request)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def malformed_address(request: Request, exc: RequestValidationError) -> Response:
        """A run link cut short (its id no longer an id) is a page that doesn't exist, as far as a browser goes."""
        if wants_a_page(request) and all(error["loc"][0] == "path" for error in exc.errors()):
            return await run_in_threadpool(common.missing_page, request)
        return await request_validation_exception_handler(request, exc)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(web.router)
    return app


app = create_app()
