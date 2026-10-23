"""Deterministic stand-ins for every provider (ADR 0003).

Used by the tests, local development and preview environments (`PROVIDERS=fake`), so the whole pipeline runs end to
end without spending money. Everything is synthetic: names, companies and emails are generated, and every email uses
the reserved `example.com` domain.

Output depends only on the inputs (seeded with SHA-256, not Python's per-process `hash`), so a test or a QA run sees
the same data every time.
"""

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.providers.types import (
    Answer,
    EnrichedPerson,
    FetchedPage,
    MatchResult,
    OutgoingEmail,
    SearchedPost,
    SearchRun,
    SearchStatus,
)
from app.providers.webfetch import FetchError, extract_page
from app.schemas import (
    AttendanceLabel,
    EventSuggestion,
    Icp,
    LabeledPost,
    Persona,
    PostToLabel,
    Seniority,
    SiteAnalysis,
    SizeBand,
)

FAKE_POST_PRICE_USD = Decimal("0.002")


def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(seed[:8], "big"))  # noqa: S311 (test data, not security)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ------------------------------------------------------------------------------------------------------ website
DEFAULT_HTML = """<!doctype html><html><head><title>{name} | Revenue analytics for B2B SaaS</title>
<meta name="description" content="{name} gives B2B SaaS revenue teams one view of pipeline."></head>
<body><nav><a href="/about">About</a> <a href="/customers">Customers</a> <a href="/pricing">Pricing</a></nav>
<h1>Pipeline you can trust</h1><p>{name} helps marketing and RevOps leaders at B2B SaaS companies with 50 to 1,000
employees see which campaigns create pipeline. Trusted by growth teams across North America and Europe.</p>
</body></html>"""


class FakeFetcher:
    """Serves a generic B2B SaaS site for any public-looking URL; `.invalid` hosts fail like an unreachable site."""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self._pages = pages or {}
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.fetched.append(url)
        if ".invalid" in url:
            raise FetchError("example.invalid could not be found")
        html = self._pages.get(url) or DEFAULT_HTML.format(name="Acme Analytics")
        return extract_page(url, 200, "text/html", html)


# ------------------------------------------------------------------------------------------------------ the model
class FakeLlm:
    """Answers like the model would, from simple rules over the input."""

    def analyze_site(self, pages: list[FetchedPage]) -> Answer[SiteAnalysis]:
        if not pages:
            raise ValueError("no pages to analyze")
        title = pages[0].title or "Your company"
        name = title.split("|")[0].strip() or "Your company"
        analysis = SiteAnalysis(
            company_name=name,
            offering=f"{name} sells revenue analytics software to B2B SaaS companies.",
            icp=Icp(
                summary="Marketing and RevOps leaders at B2B SaaS companies with 50 to 1,000 employees.",
                industries=["Software", "Information Technology"],
                company_sizes=[SizeBand.S51_200, SizeBand.S201_500, SizeBand.S501_1000],
                regions=["United States", "Canada", "United Kingdom"],
                personas=[
                    Persona(
                        label="Marketing leaders",
                        title_keywords=["marketing", "demand gen", "growth"],
                        seniorities=[Seniority.VP, Seniority.HEAD, Seniority.DIRECTOR, Seniority.C_SUITE],
                    ),
                    Persona(
                        label="Revenue operations",
                        title_keywords=["revops", "revenue operations", "sales operations"],
                        seniorities=[Seniority.HEAD, Seniority.DIRECTOR, Seniority.MANAGER],
                    ),
                ],
                exclude_title_keywords=["student", "intern", "recruiter"],
            ),
            event_topics=["B2B marketing", "revenue operations", "SaaS growth"],
        )
        return Answer(analysis, model="fake")

    def find_events(self, analysis: SiteAnalysis, *, today: date) -> Answer[list[EventSuggestion]]:
        year = today.year
        plans = [
            ("RevOps Summit", "RevOpsSummit", -40, "Austin, United States"),
            ("B2B Growth Forum", "B2BGrowth", -12, "Toronto, Canada"),
            ("Pipeline Week", "PipelineWeek", 21, "Online"),
            ("SaaS Marketing Conference", "SaaSMarketingConf", 55, "London, United Kingdom"),
        ]
        events = []
        for name, tag, offset, location in plans:
            start = today + timedelta(days=offset)
            slug = _slug(name)
            events.append(
                EventSuggestion(
                    name=f"{name} {year}",
                    short_name=name,
                    hashtags=[f"{tag}{year}", tag],
                    start_date=start,
                    end_date=start + timedelta(days=1),
                    location=location,
                    url=f"https://{slug}.example.com/",
                    source_url=f"https://search.example.com/{slug}",
                    why=f"Where {analysis.icp.personas[0].label.lower()} in {analysis.event_topics[0]} meet.",
                )
            )
        return Answer(events, model="fake")

    def label_posts(self, event_name: str, posts: list[PostToLabel]) -> Answer[list[LabeledPost]]:
        return Answer([_fake_label(event_name, p) for p in posts], model="fake")


_FAKE_LABEL_RULES = [
    (r"can't make it|not going|following .* online", AttendanceLabel.NOT_ATTENDING),
    (r"registration|early-bird|tickets|discount code|use code", AttendanceLabel.PROMOTING),
    (r"\bbooth\b", AttendanceLabel.EXHIBITING),
    (r"speaking|on stage|my session|panel", AttendanceLabel.SPEAKING),
    (r"back from|takeaways|what a week|recap", AttendanceLabel.ATTENDED),
    (r"heading to|see you at|i'll be at|attending", AttendanceLabel.ATTENDING),
]


def _fake_label(event_name: str, post: PostToLabel) -> LabeledPost:
    for pattern, label in _FAKE_LABEL_RULES:
        if re.search(pattern, post.text, re.IGNORECASE):
            reason = f"{event_name}: matched '{pattern}'"
            return LabeledPost(post_id=post.post_id, label=label, confidence=0.9, reason=reason)
    reason = f"{event_name}: no clear signal"
    return LabeledPost(post_id=post.post_id, label=AttendanceLabel.UNCLEAR, confidence=0.4, reason=reason)


# ---------------------------------------------------------------------------------------------------- post search
FIRST_NAMES = ["Avery", "Jordan", "Riley", "Morgan", "Casey", "Quinn", "Rowan", "Sage", "Emerson", "Harper"]
LAST_NAMES = ["Lindqvist", "Okafor", "Moreau", "Tanaka", "Castellano", "Novak", "Haddad", "Sorensen", "Vega", "Ito"]
TITLES = [
    "VP of Marketing",
    "Head of Growth",
    "Director of Demand Generation",
    "Revenue Operations Manager",
    "Chief Marketing Officer",
    "Account Executive",
    "Software Engineer",
    "Marketing Student",
    "Technical Recruiter",
]
COMPANY_PAGE_EVERY = 8  # one post in eight comes from a company page, as in real searches
COMPANIES = ["Northwind Labs", "Bluefin Software", "Tallgrass Data", "Copperline", "Fieldstone AI", "Brightpath"]
POST_TEMPLATES = [
    "Heading to {event} next week! If you're there, let's grab a coffee.",
    "See you at {event}! I'll be around both days.",
    "Back from {event}. My three takeaways on pipeline and AI agents.",
    "What a week at {event}. Great conversations with RevOps leaders.",
    "I'm speaking at {event} on Thursday about measuring pipeline.",
    "Visit us at booth 42 at {event} to see our new release.",
    "Registration for {event} is open. Early-bird tickets end Friday, use code SAVE20.",
    "Can't make it to {event} this year, but following the sessions online.",
    "Some thoughts on where {event} fits in the conference calendar.",
]


def _event_in(query: str) -> str:
    quoted = re.search(r'"([^"]+)"', query)
    if quoted:
        return quoted.group(1)
    return query.lstrip("#").split(" AND ")[0].strip()


@dataclass
class FakePostSearch:
    """Finishes after `polls_until_done` status checks and returns a realistic mix of posts for each query."""

    polls_until_done: int = 1
    posts_per_query: int = 9
    failing_queries: set[str] = field(default_factory=set)
    _polls: dict[str, int] = field(default_factory=dict)
    _queries: dict[str, list[str]] = field(default_factory=dict)
    _limits: dict[str, tuple[int, Decimal]] = field(default_factory=dict)

    def start(self, queries: list[str], *, max_posts_per_query: int, max_charge_usd: Decimal) -> SearchRun:
        run_id = "fake-" + hashlib.sha256("|".join(queries).encode()).hexdigest()[:20]
        self._polls[run_id] = 0
        self._queries[run_id] = list(queries)
        self._limits[run_id] = (min(self.posts_per_query, max_posts_per_query), max_charge_usd)
        return SearchRun(id=run_id, status=SearchStatus.RUNNING)

    def _limit(self, run_id: str) -> tuple[int, Decimal]:
        return self._limits.get(run_id, (self.posts_per_query, Decimal("Infinity")))

    def get(self, run_id: str) -> SearchRun:
        queries = self._queries.get(run_id, [])
        if any(q in self.failing_queries for q in queries):
            return SearchRun(id=run_id, status=SearchStatus.FAILED, message="fake failure")
        self._polls[run_id] = self._polls.get(run_id, 0) + 1
        if self._polls[run_id] < self.polls_until_done:
            return SearchRun(id=run_id, status=SearchStatus.RUNNING)
        per_query, max_charge = self._limit(run_id)
        cost = min(FAKE_POST_PRICE_USD * per_query * max(len(queries), 1), max_charge)
        return SearchRun(id=run_id, status=SearchStatus.SUCCEEDED, dataset_id=run_id, cost_usd=cost)

    def posts(self, run: SearchRun) -> list[SearchedPost]:
        per_query, _ = self._limit(run.id)
        out: list[SearchedPost] = []
        for query in self._queries.get(run.id, [run.id]):
            event = _event_in(query)
            out.extend(_fake_post(event, query, i) for i in range(per_query))
        return out


def _fake_post(event: str, query: str, index: int) -> SearchedPost:
    rng = _rng(event, index)
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    title = rng.choice(TITLES)
    company = rng.choice(COMPANIES)
    is_company = index % COMPANY_PAGE_EVERY == COMPANY_PAGE_EVERY - 1
    handle = _slug(f"{first} {last} {rng.randrange(1000, 9999)}")
    posted = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=rng.randrange(0, 24 * 40))
    return SearchedPost(
        id=hashlib.sha256(f"{event}|{index}".encode()).hexdigest()[:19],
        url=f"https://www.linkedin.com/posts/{handle}-activity-{index}",
        text=POST_TEMPLATES[index % len(POST_TEMPLATES)].format(event=event),
        author_name=company if is_company else f"{first} {last}",
        author_url=f"https://www.linkedin.com/company/{_slug(company)}"
        if is_company
        else f"https://www.linkedin.com/in/{handle}",
        author_headline=None if is_company else f"{title} at {company}",
        author_kind="company" if is_company else "person",
        posted_at=posted,
        query=query,
    )


# ------------------------------------------------------------------------------------------------------ enrichment
class FakeEnrichment:
    """About 85% of people are found; of those, about three in four have a verified email."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def match(self, linkedin_urls: list[str]) -> MatchResult:
        if len(linkedin_urls) > 10:  # noqa: PLR2004 (Apollo's batch limit)
            raise ValueError("at most 10 people per call")
        self.calls.append(list(linkedin_urls))
        people = [_fake_person(url) for url in linkedin_urls]
        return MatchResult(people=people, credits_used=sum(1 for p in people if p is not None))


def _fake_person(url: str) -> EnrichedPerson | None:
    rng = _rng("person", url)
    if rng.random() < 0.15:  # noqa: PLR2004
        return None
    handle = url.rstrip("/").rsplit("/", 1)[-1]
    first, last = [*handle.split("-"), "x", "y"][:2]
    title = rng.choice(TITLES)
    company = rng.choice(COMPANIES)
    verified = rng.random() < 0.75  # noqa: PLR2004
    seniority = (
        "vp" if "VP" in title else "head" if "Head" in title else "director" if "Director" in title else "manager"
    )
    return EnrichedPerson(
        linkedin_url=url,
        apollo_id="fake" + hashlib.sha256(url.encode()).hexdigest()[:20],
        name=f"{first.title()} {last.title()}",
        headline=f"{title} at {company}",
        title=title,
        seniority=seniority,
        departments=["master_marketing"],
        email=f"{first}.{last}@example.com",
        email_status="verified" if verified else "unavailable",
        email_domain_catchall=False,
        company_name=company,
        company_domain=f"{_slug(company)}.example.com",
        company_industry=rng.choice(["computer software", "information technology & services", "marketing"]),
        company_size=rng.choice([25, 120, 340, 800, 2500]),
        country=rng.choice(["United States", "Canada", "United Kingdom"]),
    )


# ------------------------------------------------------------------------------------------- email check and mail
class FakeEmailVerifier:
    def verify(self, email: str) -> str:
        return "invalid" if email.startswith("bounce") else "safe"


class FakeMailer:
    """Keeps sent emails in memory so tests can read the sign-in link out of them."""

    def __init__(self) -> None:
        self.outbox: list[OutgoingEmail] = []

    def send(self, email: OutgoingEmail) -> None:
        self.outbox.append(email)

    def last_to(self, address: str) -> OutgoingEmail | None:
        return next((e for e in reversed(self.outbox) if e.to == address), None)
