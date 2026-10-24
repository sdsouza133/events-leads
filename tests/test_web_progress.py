"""A working run's page shows how far it has got as a few steps (issue #8): the plan's two while it's made, the
search's three once it's approved."""

import pytest

from app.models import Run, RunStatus
from app.pipeline import analyze, collect, qualify
from app.web import run_page, start


def working(status: RunStatus, detail: str, *, icp: dict[str, object] | None = None) -> Run:
    return Run(domain="acmewidgets.com", status=status, status_detail=detail, icp=icp)


def test_the_plan_starts_by_reading_the_website() -> None:
    found = run_page.progress(working(RunStatus.ANALYZING, start.READING_SITE))
    assert found == run_page.Progress(
        "plan", ["Reading acmewidgets.com", "Finding events your buyers go to"], 0, note=""
    )


def test_once_the_website_is_read_the_events_are_next() -> None:
    found = run_page.progress(working(RunStatus.ANALYZING, analyze.FINDING_EVENTS, icp={"company_name": "Acme"}))
    assert found is not None
    assert (found.phase, found.step, found.note) == ("plan", 1, "")


@pytest.mark.parametrize(
    ("status", "detail", "step"),
    [
        (RunStatus.COLLECTING, collect.SEARCHING, 0),
        (RunStatus.QUALIFYING, collect.READING_POSTS, 1),
        (RunStatus.ENRICHING, qualify.ENRICHING, 2),
    ],
)
def test_the_search_has_three_steps(status: RunStatus, detail: str, step: int) -> None:
    found = run_page.progress(working(status, detail))
    assert found is not None
    assert (found.phase, found.steps, found.step, found.note) == ("search", run_page.SEARCH_STEPS, step, "")


def test_waiting_for_capacity_is_said_in_a_line_of_its_own() -> None:
    found = run_page.progress(working(RunStatus.WAITING_FOR_BUDGET, collect.WAITING))
    assert found is not None
    assert (found.step, found.note) == (0, collect.WAITING)


@pytest.mark.parametrize(
    "status",
    [RunStatus.AWAITING_SIGN_IN, RunStatus.AWAITING_APPROVAL, RunStatus.READY, RunStatus.FAILED, RunStatus.CANCELED],
)
def test_a_run_that_is_not_working_has_no_steps(status: RunStatus) -> None:
    assert run_page.progress(working(status, "")) is None


def test_every_status_has_a_heading() -> None:
    assert set(run_page.LABELS) == set(RunStatus)
