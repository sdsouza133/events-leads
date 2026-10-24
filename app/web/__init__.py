"""The requester's pages (issue #8, ADR 0005): rendered on the server, with no third-party scripts."""

from fastapi import APIRouter

from app.web import outbox, run_page, start

router = APIRouter()
router.include_router(start.router)
router.include_router(run_page.router)
router.include_router(outbox.router)
