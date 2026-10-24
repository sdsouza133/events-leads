"""The requester's pages (issue #8, ADR 0005) and the operator console (issue #9): rendered on the server, with no
third-party scripts."""

from fastapi import APIRouter

from app.web import console, operator_api, outbox, plan, results, run_page, start

router = APIRouter()
router.include_router(start.router)
router.include_router(run_page.router)
router.include_router(plan.router)
router.include_router(results.router)
router.include_router(outbox.router)
router.include_router(console.router)
router.include_router(operator_api.router)
