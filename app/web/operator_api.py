"""The operator API (issue #9): in manual AI mode, the questions for Claude that are waiting on an operator, and a
way to answer them. Every request carries OPERATOR_API_TOKEN as a bearer token; while it isn't set, there's no API.

An answer must fit the schema Claude's answer would have to (`app/providers/manual.py`), and make sense for its
question: events in the window it asks about, exactly one label for each post. The job that asked picks the answer
up within a couple of minutes.
"""

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.models import AiTask, Run
from app.providers import manual
from app.web import common
from app.web.common import AppSettings, DbSession, PostedJson

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api")

TASKS_SHOWN = 50
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/tasks", response_model=None)
def open_tasks(request: Request, *, session: DbSession, settings: AppSettings) -> JSONResponse:
    """The oldest questions still waiting, each with what it asks, the run waiting on it and its answer's schema."""
    refused = _refused(request, settings)
    if refused is not None:
        return refused
    rows = session.execute(
        select(AiTask, Run.domain, Run.status)
        .outerjoin(Run, Run.id == AiTask.run_id)
        .where(AiTask.answer.is_(None))
        .order_by(AiTask.id)
        .limit(TASKS_SHOWN)
    )
    tasks = [
        {
            "id": task.id,
            "kind": task.kind,
            "asked_at": task.created_at.isoformat(),
            "run": None if task.run_id is None else {"id": str(task.run_id), "domain": domain, "status": status},
            "question": task.question,
            "answer_schema": manual.SHAPES[task.kind].model_json_schema(),
        }
        for task, domain, status in rows
    ]
    return JSONResponse({"tasks": tasks}, headers=NO_STORE)


@router.post("/tasks/{task_id}", response_model=None)
def answer_task(
    request: Request, task_id: int, *, session: DbSession, settings: AppSettings, body: PostedJson
) -> JSONResponse:
    refused = _refused(request, settings)
    if refused is not None:
        return refused
    task = session.get(AiTask, task_id, with_for_update=True)
    if task is None:
        return _error("no such task", 404)
    if task.answer is not None:
        return _error("this task has been answered already", 409)
    try:
        answer = manual.SHAPES[task.kind].model_validate(body)
    except ValidationError as exc:  # the problems name where, not what was sent: answers can quote people's posts
        where = [".".join(str(part) for part in error["loc"]) or "(root)" for error in exc.errors()]
        problems = [f"{place}: {error['msg']}" for place, error in zip(where, exc.errors(), strict=True)]
        return _error("the answer doesn't fit the schema", 422, problems)
    problems = manual.problems(task.question, answer)
    if problems:
        return _error("the answer doesn't fit the question", 422, problems)
    task.answer, task.answered_at = answer.model_dump(mode="json"), common.now()
    session.commit()
    log.info("ai task answered", extra={"task_id": task.id, "kind": task.kind})
    return JSONResponse({"id": task.id, "status": "answered"}, headers=NO_STORE)


def _refused(request: Request, settings: Settings) -> JSONResponse | None:
    """None for a request with the operator token; otherwise the answer to give instead."""
    expected = settings.operator_api_token
    if expected is None:
        return _error("not found", 404)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.strip().encode(), expected.get_secret_value().encode()
    ):
        response = _error("this needs the operator token, as a bearer token", 401)
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    return None


def _error(message: str, status_code: int, problems: list[str] | None = None) -> JSONResponse:
    body: dict[str, object] = {"error": message}
    if problems:
        body["problems"] = problems
    return JSONResponse(body, status_code=status_code, headers=NO_STORE)
