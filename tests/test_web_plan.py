"""The plan page (issue #8, FR-4): edit the ICP, pick up to 3 events, add your own, edit the searches, approve.

The tests submit the form as rendered, the way a browser does, after changing only what a requester would change.
"""

import html
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.config import Settings, get_settings
from app.main import create_app
from app.models import Account, Event, Job, Run, RunEvent, RunStatus
from app.pipeline.collect import COLLECT_EVENT
from app.pipeline.keywords import build_queries
from app.schemas import EventSuggestion, SiteAnalysis
from app.web import plan
from tests.factories import make_run

pytestmark = pytest.mark.db

TODAY = datetime.now(UTC).date()
ANALYSIS = SiteAnalysis.model_validate(
    {
        "company_name": "Acme Widgets",
        "offering": "Acme sells pipeline analytics to B2B SaaS companies.",
        "icp": {
            "summary": "Marketing and RevOps leaders at mid-size B2B SaaS companies.",
            "industries": ["Software", "Information Technology"],
            "company_sizes": ["51-200", "201-500"],
            "regions": ["United States", "Canada"],
            "personas": [
                {"label": "Marketing leaders", "title_keywords": ["marketing", "demand gen"], "seniorities": ["vp"]},
                {"label": "Revenue operations", "title_keywords": ["revops"], "seniorities": ["head", "manager"]},
            ],
            "exclude_title_keywords": ["student", "recruiter"],
        },
        "event_topics": ["B2B marketing"],
    }
)
EVENTS = [
    EventSuggestion(
        name=f"{name} {TODAY.year}",
        short_name=name,
        hashtags=[name.replace(" ", "")],
        start_date=TODAY + timedelta(days=offset),
        end_date=TODAY + timedelta(days=offset + 1),
        location=where,
        url=f"https://{name.lower().replace(' ', '-')}.example.com/",
        source_url=f"https://search.example.com/{offset}",
        why="Where your buyers meet.",
    )
    for name, offset, where in [
        ("RevOps Summit", -20, "Austin, United States"),
        ("Pipeline Week", 10, "Online"),
        ("B2B Growth Forum", 30, "Toronto, Canada"),
        ("SaaS Marketing Conference", 60, "London, United Kingdom"),
    ]
]
OPERATOR = "ops@b2bops.com"
Fields = dict[str, list[str]]


class _Form(HTMLParser):
    """The fields a browser sends for one form: hidden and text inputs, textareas, and ticked checkboxes."""

    def __init__(self, form_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.inside = False
        self.fields: Fields = {}
        self._textarea: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "form" and found.get("id") == self.form_id:
            self.inside = True
        name = found.get("name")
        if not self.inside or not name:
            return
        kind = found.get("type") or "text"
        if tag == "input" and kind not in ("submit", "button") and (kind != "checkbox" or "checked" in found):
            self.fields.setdefault(name, []).append(found.get("value") or "")
        elif tag == "textarea":
            self._textarea, self._text = name, []

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._textarea is not None:
            text = "".join(self._text)
            self.fields.setdefault(self._textarea, []).append(text.removeprefix("\n"))
            self._textarea = None
        elif tag == "form":
            self.inside = False


@pytest.fixture
def settings() -> Settings:
    return Settings(admin_emails=OPERATOR)


@pytest.fixture
def client(db: Session, settings: Settings) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


def planned_run(db: Session, *, suggestions: int = 4) -> Run:
    """A run whose plan analyze just proposed, as analyze leaves it."""
    run = make_run(db, domain="acmewidgets.com", status=RunStatus.AWAITING_APPROVAL)
    run.icp = ANALYSIS.model_dump(mode="json")
    run.suggested_events = [
        {**event.model_dump(mode="json"), "queries": build_queries(event, today=TODAY)}
        for event in EVENTS[:suggestions]
    ]
    db.commit()
    return run


def sign_in_as(client: TestClient, db: Session, account_id: uuid.UUID) -> None:
    cookie = auth.open_session(db, account_id, now=datetime.now(UTC), lifetime=timedelta(days=1))
    db.commit()
    client.cookies.set(auth.SESSION_COOKIE, cookie)


def opened(client: TestClient, db: Session, run: Run) -> Fields:
    """The requester opens their run's page: the plan, as its form would be sent."""
    sign_in_as(client, db, run.account_id)
    response = client.get(f"/runs/{run.id}")
    assert response.status_code == 200
    return form_of(response.text)


def form_of(page: str) -> Fields:
    parser = _Form("plan")
    parser.feed(page)
    return parser.fields


def press(
    client: TestClient, run: Run, fields: Fields, button: str, changes: dict[str, str | list[str] | None] | None = None
) -> httpx2.Response:
    """Change some fields (None clears a field, or unticks a checkbox) and press a button."""
    data = {name: list(values) for name, values in fields.items()}
    for name, value in (changes or {}).items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value if isinstance(value, list) else [value]
    data["action"] = [button]
    return client.post(f"/runs/{run.id}/plan", data=data, follow_redirects=False)


def fresh(db: Session, run: Run) -> Run:
    db.expire_all()
    found = db.get(Run, run.id)
    assert found is not None
    return found


def searched(db: Session, run: Run) -> dict[str, list[str]]:
    """The events the run is searching, with their searches, by event name."""
    db.expire_all()
    rows = db.execute(select(Event.name, RunEvent.queries).join(Event).where(RunEvent.run_id == run.id))
    return {name: list(queries) for name, queries in rows}


def text(response: httpx2.Response) -> str:
    """The page's text as a person reads it, with &#39; and the like turned back into characters."""
    return html.unescape(response.text)


# ------------------------------------------------------------------------------------------------ the page
def test_the_plan_shows_what_we_found_and_the_first_three_events_are_picked(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    fields = opened(client, db, run)
    assert fields["summary"] == [ANALYSIS.icp.summary]
    assert fields["industries"] == ["Software, Information Technology"]
    assert fields["company_sizes"] == ["51-200", "201-500"]
    assert fields["buyer-0-keywords"] == ["marketing, demand gen"]
    assert fields["buyer-1-seniorities"] == ["head", "manager"]
    assert fields["buyer-2-label"] == [""]  # one empty buyer, for adding
    assert [name for name in fields if name.endswith("-pick")] == ["event-0-pick", "event-1-pick", "event-2-pick"]
    assert fields["event-0-queries"] == ["\n".join(build_queries(EVENTS[0], today=TODAY))]
    page = client.get(f"/runs/{run.id}").text
    assert "Acme sells pipeline analytics" in page
    assert "Pipeline Week" in page
    assert "Online" in page


@pytest.mark.parametrize(
    ("start", "end", "shown"),
    [
        (date(2026, 11, 12), None, "12 Nov 2026"),
        (date(2026, 11, 12), date(2026, 11, 13), "12\u201313 Nov 2026"),
        (date(2026, 11, 30), date(2026, 12, 2), "30 Nov \u2013 2 Dec 2026"),
        (date(2026, 12, 30), date(2027, 1, 2), "30 Dec 2026 \u2013 2 Jan 2027"),
        (None, None, ""),
    ],
)
def test_event_dates_are_written_out(start: date | None, end: date | None, shown: str) -> None:
    assert plan._when(start, end) == shown


# --------------------------------------------------------------------------------------------- approving
def test_approving_the_plan_as_it_stands_searches_the_picked_events(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    response = press(client, run, opened(client, db, run), "approve")
    assert (response.status_code, response.headers["location"]) == (303, f"/runs/{run.id}")

    approved = fresh(db, run)
    assert approved.status == RunStatus.COLLECTING
    assert approved.approved_at is not None
    assert searched(db, run) == {event.name: build_queries(event, today=TODAY) for event in EVENTS[:3]}
    assert len(db.scalars(select(Job).where(Job.kind == COLLECT_EVENT)).all()) == 3
    assert "Finding posts about your events" in client.get(f"/runs/{run.id}").text


def test_the_requester_s_edits_are_what_runs(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    fields = opened(client, db, run)
    edits: dict[str, str | list[str] | None] = {
        "industries": "Software, Fintech, software",
        "company_sizes": ["51-200"],
        "buyer-1-label": "",  # Revenue operations, removed
        "buyer-1-keywords": "",
        "buyer-1-seniorities": None,
        "buyer-2-label": "Founders",
        "buyer-2-keywords": "founder, ceo",
        "buyer-2-seniorities": ["founder", "c_suite"],
        "exclude": "student",
        "event-0-queries": '"RevOps Summit"\n#RevOpsSummit\n\n#revopssummit',
        "event-2-pick": None,
    }
    assert press(client, run, fields, "approve", edits).status_code == 303

    icp = SiteAnalysis.model_validate(fresh(db, run).icp).icp
    assert icp.industries == ["Software", "Fintech"]
    assert icp.company_sizes == ["51-200"]
    assert [(p.label, p.title_keywords, p.seniorities) for p in icp.personas] == [
        ("Marketing leaders", ["marketing", "demand gen"], ["vp"]),
        ("Founders", ["founder", "ceo"], ["founder", "c_suite"]),
    ]
    assert icp.exclude_title_keywords == ["student"]
    assert searched(db, run) == {
        EVENTS[0].name: ['"RevOps Summit"', "#RevOpsSummit"],  # a search typed twice is run once
        EVENTS[1].name: build_queries(EVENTS[1], today=TODAY),
    }


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"summary": " "}, "summary", "Describe who you sell to"),
        ({"industries": " , "}, "industries", "List 1 to 12 industries"),
        ({"company_sizes": None}, "company_sizes", "Pick at least one company size"),
        ({"regions": ""}, "regions", "List 1 to 12 countries or regions"),
        ({"buyer-0-keywords": ""}, "buyer-0", "Give this buyer a name and 1 to 15 words"),
        ({"buyer-2-keywords": "founder"}, "buyer-2", "Give this buyer a name and 1 to 15 words"),
        (
            {"buyer-0-label": "", "buyer-0-keywords": "", "buyer-1-label": "", "buyer-1-keywords": ""},
            "buyers",
            "Describe 1 to 5 kinds of buyer",
        ),
        ({"exclude": ", ".join(f"word{i}" for i in range(21))}, "exclude", "List up to 20 words"),
        ({"event-0-queries": ""}, "event-0", "Give this event 1 to 4 searches"),
        ({"event-1-queries": "\n".join(f'"search {i}"' for i in range(5))}, "event-1", "1 to 4 searches"),
        ({"event-3-queries": "x" * 301}, "event-3", "1 to 4 searches"),  # unpicked, but not stored as typed
        ({"event-3-pick": "1"}, "events", "Pick up to 3 events"),
        ({"event-0-pick": None, "event-1-pick": None, "event-2-pick": None}, "events", "Pick at least one event"),
    ],
)
def test_each_problem_is_explained_next_to_its_field_and_nothing_is_saved(
    client: TestClient, db: Session, changes: dict[str, str | list[str] | None], field: str, message: str
) -> None:
    run = planned_run(db)
    response = press(client, run, opened(client, db, run), "approve", changes)
    assert response.status_code == 400
    assert f'href="#{field}"' in response.text  # in the summary at the top, linked to the field
    assert f'id="{field}-error"' in response.text
    assert message in text(response)
    unchanged = fresh(db, run)
    assert unchanged.status == RunStatus.AWAITING_APPROVAL
    assert unchanged.icp == ANALYSIS.model_dump(mode="json")


def test_a_half_filled_buyer_is_pointed_at_rather_than_the_whole_list(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    changes: dict[str, str | list[str] | None] = {"buyer-0-keywords": "", "buyer-1-label": "", "buyer-1-keywords": ""}
    response = press(client, run, opened(client, db, run), "approve", changes)
    assert response.status_code == 400
    assert 'href="#buyer-0"' in response.text
    assert 'href="#buyers"' not in response.text  # "describe at least one buyer" would point at the wrong thing
    assert response.text.count("buyer-0-error") == 4  # the message, and its group and both fields pointing to it


def test_a_second_approval_changes_nothing(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    fields = opened(client, db, run)
    assert press(client, run, fields, "approve").status_code == 303
    again = press(client, run, fields, "approve", {"event-3-pick": "1", "event-0-pick": None})
    assert (again.status_code, again.headers["location"]) == (303, f"/runs/{run.id}")
    assert set(searched(db, run)) == {event.name for event in EVENTS[:3]}


def test_saving_without_approving_keeps_the_edits(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    response = press(client, run, opened(client, db, run), "add", {"regions": "United States", "event-2-pick": None})
    assert (response.status_code, response.headers["location"]) == (303, f"/runs/{run.id}?notice=saved#plan")

    page = client.get(response.headers["location"])
    assert "Your changes are saved." in page.text
    fields = form_of(page.text)
    assert fields["regions"] == ["United States"]
    assert "event-2-pick" not in fields
    assert fresh(db, run).status == RunStatus.AWAITING_APPROVAL


# ------------------------------------------------------------------------------------- events of your own
def test_an_event_of_your_own_gets_searches_and_is_picked_if_there_is_room(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    when = TODAY + timedelta(days=45)
    changes: dict[str, str | list[str] | None] = {
        "event-2-pick": None,
        "new_name": "Revenue  Leaders Europe",
        "new_date": when.isoformat(),
        "new_url": "revenue-leaders.example.com/2026",
    }
    response = press(client, run, opened(client, db, run), "add", changes)
    assert response.headers["location"] == f"/runs/{run.id}?notice=added#events"
    page = client.get(response.headers["location"]).text
    assert "We added your event and picked it." in page
    assert "Revenue Leaders Europe (added by you)" in page

    own = (fresh(db, run).suggested_events or [])[-1]
    assert (own["name"], own["start_date"], own["url"], own["source"], own["picked"]) == (
        "Revenue Leaders Europe",
        when.isoformat(),
        "https://revenue-leaders.example.com/2026",
        "requester",
        True,
    )
    assert own["queries"][0] == '"Revenue Leaders Europe"'

    assert press(client, run, form_of(page), "approve").status_code == 303
    assert searched(db, run)["Revenue Leaders Europe"] == own["queries"]
    db.expire_all()
    sources = db.scalars(select(RunEvent.source).where(RunEvent.run_id == run.id)).all()
    assert sorted(sources) == ["requester", "suggested", "suggested"]


def test_an_event_added_with_three_already_picked_waits_unpicked(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    response = press(client, run, opened(client, db, run), "add", {"new_name": "Revenue Leaders Europe"})
    assert response.headers["location"] == f"/runs/{run.id}?notice=added-unpicked#events"
    assert "untick one to search this one instead" in text(client.get(response.headers["location"]))
    assert (fresh(db, run).suggested_events or [])[-1]["picked"] is False


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"new_date": "2026-11-12"}, "new_name", "Give the event's name."),
        ({"new_name": "x" * 121}, "new_name", "in up to 120 characters"),
        ({"new_name": "Ops Day", "new_date": "12/11/2026"}, "new_date", "Enter the date as YYYY-MM-DD."),
        ({"new_name": "Ops Day", "new_url": "ftp://ops-day.example.com"}, "new_url", "Enter the event's web address"),
        (
            {"new_name": f"revops summit {TODAY.year}", "new_date": EVENTS[0].start_date.isoformat()},  # type: ignore[union-attr]
            "new_name",
            "That event is already in your list.",
        ),
    ],
)
def test_an_event_of_your_own_needs_a_name_and_a_usable_date_and_address(
    client: TestClient, db: Session, changes: dict[str, str | list[str] | None], field: str, message: str
) -> None:
    run = planned_run(db)
    response = press(client, run, opened(client, db, run), "add", changes)
    assert response.status_code == 400
    assert f'id="{field}-error"' in response.text
    assert message in text(response)
    assert len(fresh(db, run).suggested_events or []) == len(EVENTS)


def test_three_events_of_your_own_at_most(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    fields = opened(client, db, run)
    for name in ["Ops Day", "Growth Night", "Demand Week"]:
        response = press(client, run, fields, "add", {"new_name": name})
        assert response.status_code == 303
        fields = form_of(client.get(response.headers["location"]).text)
    response = press(client, run, fields, "add", {"new_name": "One Too Many"})
    assert response.status_code == 400
    assert "You can add up to 3 events of your own." in text(response)


def test_an_event_typed_but_not_added_is_not_approved_by_mistake(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    response = press(client, run, opened(client, db, run), "approve", {"new_name": "Ops Day"})
    assert response.status_code == 400
    assert 'Press "Add this event" to add it to the list first' in text(response)
    assert fresh(db, run).status == RunStatus.AWAITING_APPROVAL


def test_without_suggestions_the_requester_adds_their_own_event(client: TestClient, db: Session) -> None:
    run = planned_run(db, suggestions=0)
    fields = opened(client, db, run)
    assert "We didn't find events from the last 60 days or the next 90." in text(client.get(f"/runs/{run.id}"))
    nothing = press(client, run, fields, "approve")
    assert nothing.status_code == 400
    assert "Pick at least one event to search, or add one of your own." in text(nothing)

    added = press(client, run, fields, "add", {"new_name": "Ops Day", "new_date": TODAY.isoformat()})
    assert added.headers["location"].endswith("notice=added#events")
    assert press(client, run, form_of(client.get(added.headers["location"]).text), "approve").status_code == 303
    assert list(searched(db, run)) == ["Ops Day"]


# ------------------------------------------------------------------------------------------ who may change it
def test_only_the_requester_or_an_operator_can_see_and_change_the_plan(
    client: TestClient, db: Session, settings: Settings
) -> None:
    run = planned_run(db)
    fields = opened(client, db, run)

    stranger = Account(email="sam@otherwidgets.com", domain="otherwidgets.com")
    operator = Account(email=OPERATOR, domain="b2bops.com")
    db.add_all([stranger, operator])
    db.commit()
    sign_in_as(client, db, stranger.id)
    assert client.get(f"/runs/{run.id}").status_code == 404
    assert press(client, run, fields, "approve").status_code == 404

    client.cookies.delete(auth.SESSION_COOKIE)
    assert press(client, run, fields, "approve").status_code == 401

    sign_in_as(client, db, operator.id)
    assert press(client, run, form_of(client.get(f"/runs/{run.id}").text), "approve").status_code == 303
    assert fresh(db, run).status == RunStatus.COLLECTING


def test_a_form_sent_without_the_page_s_token_is_refused(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    fields = opened(client, db, run)
    response = press(client, run, fields, "approve", {auth.CSRF_FIELD: "forged"})
    assert response.status_code == 400
    assert "This page had expired." in text(response)
    assert fresh(db, run).status == RunStatus.AWAITING_APPROVAL


def test_values_the_form_never_offers_are_ignored(client: TestClient, db: Session) -> None:
    run = planned_run(db)
    changes: dict[str, str | list[str] | None] = {
        "company_sizes": ["51-200", "1-1000000"],
        "buyer-0-seniorities": ["vp", "emperor"],
        "event-99-pick": "1",
    }
    assert press(client, run, opened(client, db, run), "approve", changes).status_code == 303
    icp = SiteAnalysis.model_validate(fresh(db, run).icp).icp
    assert icp.company_sizes == ["51-200"]
    assert icp.personas[0].seniorities == ["vp"]
    assert len(searched(db, run)) == 3
