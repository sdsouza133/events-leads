"""The plan a requester reviews before anything is searched (issue #8, FR-4).

While a run awaits approval, its page shows who we'll look for, as a few chips, and the events we found, as cards with
the first three picked: a plan that looks right takes one press of "Find my leads". Everything else is a click away:
"Adjust" opens the ICP to edit, each card's "Searches" the LinkedIn searches we'd run, "Show more" the other events,
and "Add an event you know" an event of the requester's own, whose searches we write from its name and date. Every
button posts the whole form, so nothing typed is lost: "Save" and "Add this event" save the plan and show it again,
"Find my leads" saves it and starts collecting.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import FormData

from app import auth
from app.config import Settings
from app.models import Account, Run, RunStatus
from app.pipeline import collect, keywords, runs
from app.schemas import (
    MAX_QUERIES,
    ApprovedEvent,
    EventFields,
    Icp,
    Persona,
    Query,
    Seniority,
    ShortText,
    SiteAnalysis,
    SizeBand,
    Url,
)
from app.web import common
from app.web.common import AppSettings, DbSession, PostedForm, SignedIn, page

router = APIRouter()

MAX_BUYERS = 5  # Icp.personas
MAX_OWN_EVENTS = 3
PRESELECTED = collect.MAX_EVENTS
"""The first suggestions start out picked, so a plan that looks right can be approved as it stands."""

NOTICES = {
    "saved": "Your changes are saved.",
    "added": "We added your event and picked it.",
    "added-unpicked": "We added your event. You've picked 3 already, so unpick one to search this one instead.",
}
EXPIRED = "This page had expired. Please press the button again."
MESSAGES = {
    "summary": "Describe who you sell to in a sentence or two (up to 600 characters).",
    "industries": "List 1 to 12 industries, separated by commas.",
    "company_sizes": "Pick at least one company size.",
    "regions": "List 1 to 12 countries or regions, separated by commas.",
    "buyers": "Describe 1 to 5 kinds of buyer.",
    "buyer": "Give this buyer a name and 1 to 15 words from their job titles, separated by commas.",
    "exclude": "List up to 20 words, separated by commas.",
    "searches": f"Give this event 1 to {MAX_QUERIES} searches, one per line.",
}
ICP_FIELDS = {  # Icp's fields, and the form field each one is edited in
    "summary": "summary",
    "industries": "industries",
    "company_sizes": "company_sizes",
    "regions": "regions",
    "personas": "buyers",
    "exclude_title_keywords": "exclude",
}
SIZES = [band.value for band in SizeBand]
CHIPS_EACH = 3
"""Industries and regions shown as chips, each: more are counted ("+2 more"), and all are in "Adjust"."""
SENIORITIES = {
    Seniority.OWNER: "Owner",
    Seniority.FOUNDER: "Founder",
    Seniority.C_SUITE: "C-suite",
    Seniority.PARTNER: "Partner",
    Seniority.VP: "VP",
    Seniority.HEAD: "Head",
    Seniority.DIRECTOR: "Director",
    Seniority.MANAGER: "Manager",
    Seniority.SENIOR: "Senior",
    Seniority.ENTRY: "Entry level",
}
APPROVED_FIELDS = ("name", "short_name", "start_date", "end_date", "location", "url", "queries")
_URL = TypeAdapter(Url)
_NAME = TypeAdapter(ShortText)
_QUERY = TypeAdapter(Query)


@dataclass
class Buyer:
    label: str = ""
    keywords: str = ""
    seniorities: list[str] = field(default_factory=list)

    @property
    def blank(self) -> bool:
        return not self.label.strip() and not self.keywords.strip()


@dataclass
class EventRow:
    name: str
    when: str
    location: str | None
    url: str | None
    why: str | None
    own: bool
    month: str  # the date tile: "NOV" over "4"
    day: str
    past: bool  # over already: we'd look for the people who went
    picked: bool
    queries: str


Numbered = list[tuple[int, EventRow]]
"""Events with their place in the form, which names their fields (event-0-pick, ...)."""


@dataclass
class Plan:
    """The plan as the form shows it: as stored, or as just posted."""

    summary: str
    industries: str
    company_sizes: list[str]
    regions: str
    buyers: list[Buyer]
    exclude: str
    events: list[EventRow]
    new_name: str = ""
    new_date: str = ""
    new_url: str = ""


def show(request: Request, run: Run, account: Account, settings: Settings, *, notice: str = "") -> HTMLResponse:
    """The run's page while it awaits approval."""
    plan = _stored(run, today=common.now().date())
    return _render(request, run, account, settings, plan=plan, notice=NOTICES.get(notice, ""))


@router.post("/runs/{run_id}/plan", response_model=None)
def save_plan(
    request: Request,
    run_id: uuid.UUID,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    form: PostedForm,
) -> Response:
    if account is None:
        return page(request, "sign_in_needed.html", {"account": None}, settings, status_code=401)
    visible = common.visible_run(session, run_id, account, settings)
    if visible is None:
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    back = RedirectResponse(f"/runs/{visible.id}", status_code=303)
    run = runs.locked(session, visible.id)  # as it is now, and no other request changes it until this one commits
    if run is None or run.status != RunStatus.AWAITING_APPROVAL:
        return back  # approved already (a second click, say) or stopped: its page says where it stands
    posted = _posted(form, run, today=common.now().date())
    if not common.csrf_ok(request, _text(form, auth.CSRF_FIELD)):
        return _render(request, run, account, settings, plan=posted, errors={"form": EXPIRED}, status_code=400)
    approving = _text(form, "action") == "approve"
    errors: dict[str, str] = {}
    icp = _icp(posted, errors)
    events = _events(run, posted, errors)
    new = _new_event(posted, events, errors, today=common.now().date())
    picked = sum(1 for event in events if event["picked"])
    if picked > collect.MAX_EVENTS:
        errors["events"] = f"Pick up to {collect.MAX_EVENTS} events."
    elif approving and new is not None:
        errors["new_name"] = 'Press "Add this event" to add it to the list first, or clear its name.'
    elif approving and picked == 0:
        errors["events"] = "Pick at least one event to search, or add one of your own."
    if errors or icp is None:
        return _render(request, run, account, settings, plan=posted, errors=errors, status_code=400)

    notice = "saved"
    if new is not None:
        new["picked"] = picked < collect.MAX_EVENTS
        events.append(new)
        notice = "added" if new["picked"] else "added-unpicked"
    run.icp = SiteAnalysis.model_validate(run.icp).model_copy(update={"icp": icp}).model_dump(mode="json")
    run.suggested_events = events
    if approving:
        approved = [_approved(event) for event in events if event["picked"]]
        collect.approve(session, run.id, approved, now=common.now())
    session.commit()  # before the redirect, so the page it leads to shows the saved plan
    shown_again = RedirectResponse(f"/runs/{run.id}?notice={notice}#{'events' if new else 'plan'}", status_code=303)
    return back if approving else shown_again


def _stored(run: Run, *, today: date) -> Plan:
    icp = SiteAnalysis.model_validate(run.icp).icp
    buyers = [
        Buyer(persona.label, ", ".join(persona.title_keywords), [str(s) for s in persona.seniorities])
        for persona in icp.personas
    ]
    events = [
        EventRow(
            **_shown(event, today=today),
            picked=bool(event.get("picked", i < PRESELECTED)),
            queries="\n".join(event.get("queries") or []),
        )
        for i, event in enumerate(run.suggested_events or [])
    ]
    return Plan(
        summary=icp.summary,
        industries=", ".join(icp.industries),
        company_sizes=[str(size) for size in icp.company_sizes],
        regions=", ".join(icp.regions),
        buyers=_one_blank(buyers),
        exclude=", ".join(icp.exclude_title_keywords),
        events=events,
    )


def _posted(form: FormData, run: Run, *, today: date) -> Plan:
    buyers = [
        Buyer(
            _text(form, f"buyer-{j}-label"),
            _text(form, f"buyer-{j}-keywords"),
            [value for value in form.getlist(f"buyer-{j}-seniorities") if isinstance(value, str)],
        )
        for j in range(MAX_BUYERS)
    ]
    events = [
        EventRow(
            **_shown(event, today=today),
            picked=_text(form, f"event-{i}-pick") == "1",
            queries=_text(form, f"event-{i}-queries"),
        )
        for i, event in enumerate(run.suggested_events or [])
    ]
    return Plan(
        summary=_text(form, "summary"),
        industries=_text(form, "industries"),
        company_sizes=[value for value in form.getlist("company_sizes") if isinstance(value, str)],
        regions=_text(form, "regions"),
        buyers=_one_blank(buyers),
        exclude=_text(form, "exclude"),
        events=events,
        new_name=_text(form, "new_name"),
        new_date=_text(form, "new_date"),
        new_url=_text(form, "new_url"),
    )


def _icp(plan: Plan, errors: dict[str, str]) -> Icp | None:
    """The edited ICP, or None with the problems in `errors`, keyed by the form field to show them next to."""
    personas = []
    for j, buyer in enumerate(plan.buyers):
        if buyer.blank:
            continue  # a buyer cleared to remove it, or the empty one for adding
        try:
            known = [s for s in buyer.seniorities if s in SENIORITIES]
            personas.append(Persona(label=buyer.label, title_keywords=_items(buyer.keywords), seniorities=known))
        except ValidationError:
            errors[f"buyer-{j}"] = MESSAGES["buyer"]
    fields = {
        "summary": plan.summary,
        "industries": _items(plan.industries),
        "company_sizes": [size for size in plan.company_sizes if size in SIZES],
        "regions": _items(plan.regions),
        "personas": personas,
        "exclude_title_keywords": _items(plan.exclude),
    }
    try:
        icp = Icp.model_validate(fields)
    except ValidationError as exc:
        for error in exc.errors():
            key = ICP_FIELDS[str(error["loc"][0])]
            if key == "buyers" and any(k.startswith("buyer-") for k in errors):
                continue  # the buyer it's about already says what's wrong
            errors.setdefault(key, MESSAGES[key])
        return None
    return icp


def _events(run: Run, plan: Plan, errors: dict[str, str]) -> list[dict[str, Any]]:
    """The stored events with the posted picks and searches. Searches must be usable, except an unpicked event's
    left empty."""
    events = []
    for i, (stored, row) in enumerate(zip(run.suggested_events or [], plan.events, strict=True)):
        queries = _lines(row.queries)
        if (row.picked or queries) and not _searches_ok(queries):
            errors[f"event-{i}"] = MESSAGES["searches"]
        events.append({**stored, "picked": row.picked, "queries": queries})
    return events


def _new_event(
    plan: Plan, events: list[dict[str, Any]], errors: dict[str, str], *, today: date
) -> dict[str, Any] | None:
    """The requester's own event to add, if they typed one, with searches written for it like ours."""
    name = " ".join(plan.new_name.split())
    if not name:
        if plan.new_date.strip() or plan.new_url.strip():
            errors["new_name"] = "Give the event's name."
        return None
    found = len(errors)
    if not _valid(_NAME, name):
        errors["new_name"] = "Give the event's name, in up to 120 characters."
    when = None
    if plan.new_date.strip():
        try:
            when = date.fromisoformat(plan.new_date.strip())
        except ValueError:
            errors["new_date"] = "Enter the date as YYYY-MM-DD."
    url = plan.new_url.strip() or None
    if url is not None and "://" not in url:
        url = f"https://{url}"
    if url is not None and not _valid(_URL, url):
        errors["new_url"] = "Enter the event's web address, like https://example.com/event."
    if sum(1 for event in events if event.get("source") == "requester") >= MAX_OWN_EVENTS:
        errors.setdefault("new_name", f"You can add up to {MAX_OWN_EVENTS} events of your own.")
    key = collect.event_key(name, when)
    if any(collect.event_key(event["name"], _day(event.get("start_date"))) == key for event in events):
        errors.setdefault("new_name", "That event is already in your list.")
    if len(errors) > found:
        return None
    basics = EventFields(name=name, start_date=when, end_date=when)
    queries = keywords.build_queries(basics, today=today)
    return {**basics.model_dump(mode="json"), "url": url, "source": "requester", "picked": False, "queries": queries}


def _approved(event: dict[str, Any]) -> ApprovedEvent:
    fields = {name: event.get(name) for name in APPROVED_FIELDS}
    return ApprovedEvent.model_validate(
        {**fields, "hashtags": event.get("hashtags") or [], "source": event.get("source", "suggested")}
    )


def _render(
    request: Request,
    run: Run,
    account: Account,
    settings: Settings,
    *,
    plan: Plan,
    errors: dict[str, str] | None = None,
    notice: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    errors = errors or {}
    shown, more = _split(plan.events, errors)
    context = {
        "account": account,
        "run": run,
        "plan": plan,
        "errors": errors,
        "notice": notice,
        "offering": (run.icp or {}).get("offering"),
        "roles": [buyer.label.strip() for buyer in plan.buyers if not buyer.blank and buyer.label.strip()],
        "firms": _firm_chips(plan),
        "adjusting": any(key in errors for key in ICP_FIELDS.values()) or any(k.startswith("buyer-") for k in errors),
        "adding": any(key in errors for key in ("new_name", "new_date", "new_url")) or not plan.events,
        "shown": shown,
        "more": more,
        "picked": sum(1 for event in plan.events if event.picked),
        "sizes": SIZES,
        "seniorities": [(str(value), label) for value, label in SENIORITIES.items()],
        "max_events": collect.MAX_EVENTS,
        "max_queries": MAX_QUERIES,
    }
    return page(request, "plan.html", context, settings, status_code=status_code)


def _split(events: list[EventRow], errors: dict[str, str]) -> tuple[Numbered, Numbered]:
    """The events shown up front, by their place in the form: the first three, and any picked, added by the
    requester, or with a problem to fix; and the rest, behind "Show more"."""
    shown: Numbered = []
    more: Numbered = []
    for i, event in enumerate(events):
        up_front = i < PRESELECTED or event.picked or event.own or f"event-{i}" in errors
        (shown if up_front else more).append((i, event))
    return shown, more


def _firm_chips(plan: Plan) -> list[str]:
    """The companies we'll look at, as chips: industries, then size, then regions."""
    chips = _some(_items(plan.industries))
    sizes = [size for size in SIZES if size in plan.company_sizes]
    if sizes:
        chips.append(_people(sizes))
    return chips + _some(_items(plan.regions))


def _some(items: list[str]) -> list[str]:
    """Up to CHIPS_EACH items, and how many more there are."""
    if len(items) <= CHIPS_EACH:
        return items
    return [*items[:CHIPS_EACH], f"+{len(items) - CHIPS_EACH} more"]


def _people(sizes: list[str]) -> str:
    """Company sizes in words: '51\u20131,000 people' when the bands run on from one another, else each band."""
    places = [SIZES.index(size) for size in sizes]
    if places == list(range(places[0], places[-1] + 1)):
        return f"{_span(_bounds(sizes[0])[0], _bounds(sizes[-1])[1])} people"
    return ", ".join(_span(*_bounds(size)) for size in sizes) + " people"


def _bounds(size: str) -> tuple[int, int | None]:
    """'51-200' -> (51, 200); '10001+' -> (10001, None)."""
    low, _, high = size.removesuffix("+").partition("-")
    return int(low), int(high) if high else None


def _span(low: int, high: int | None) -> str:
    return f"{low:,}\u2013{high:,}" if high is not None else f"{low:,}+"


def _shown(event: dict[str, Any], *, today: date) -> dict[str, Any]:
    """What the page shows of an event besides its pick and searches."""
    start, end = _day(event.get("start_date")), _day(event.get("end_date"))
    first, last = start or end, end or start
    return {
        "name": event["name"],
        "when": _when(start, end),
        "location": event.get("location"),
        "url": event.get("url"),
        "why": event.get("why"),
        "own": event.get("source") == "requester",
        "month": f"{first:%b}".upper() if first else "",
        "day": str(first.day) if first else "",
        "past": last is not None and last < today,
    }


def _when(start: date | None, end: date | None) -> str:
    """'12 Nov 2026', or a range of two dates joined by an en dash, giving the month and year once if shared."""
    start, end = start or end, end or start
    if start is None or end is None:
        return ""
    if start == end:
        return f"{start.day} {start:%b %Y}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}\u2013{end.day} {end:%b %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} \u2013 {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} \u2013 {end.day} {end:%b %Y}"


def _day(value: Any) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) and value else None


def _one_blank(buyers: list[Buyer]) -> list[Buyer]:
    """The buyers as typed, without empty ones at the end, plus one empty one for adding (up to the limit)."""
    while buyers and buyers[-1].blank:
        buyers = buyers[:-1]
    return [*buyers, Buyer()] if len(buyers) < MAX_BUYERS else buyers


def _items(text: str) -> list[str]:
    """'Software, SaaS ,software' -> ['Software', 'SaaS']: comma- or line-separated, each once."""
    seen: dict[str, str] = {}
    for item in text.replace("\n", ",").split(","):
        clean = " ".join(item.split())
        if clean:
            seen.setdefault(clean.casefold(), clean)
    return list(seen.values())


def _lines(text: str) -> list[str]:
    seen: dict[str, str] = {}
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            seen.setdefault(clean.casefold(), clean)
    return list(seen.values())


def _searches_ok(queries: list[str]) -> bool:
    return 1 <= len(queries) <= MAX_QUERIES and all(_valid(_QUERY, query) for query in queries)


def _valid(adapter: TypeAdapter[str], value: str) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""
