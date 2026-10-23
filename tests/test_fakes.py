"""The fakes stand in for paid services in tests and previews, so they must behave like them in the ways the
pipeline relies on: deterministic, realistic mixes, and the same contracts (batch limits, failures)."""

from datetime import date
from decimal import Decimal

import pytest

from app.providers.fakes import (
    FakeEmailVerifier,
    FakeEnrichment,
    FakeFetcher,
    FakeLlm,
    FakeMailer,
    FakePostSearch,
)
from app.providers.protocols import EmailVerifier, Llm, Mailer, PeopleEnrichment, PostSearch, WebFetcher
from app.providers.types import OutgoingEmail, SearchStatus
from app.providers.webfetch import FetchError
from app.schemas import AttendanceLabel, PostToLabel

TODAY = date(2026, 10, 23)


def test_fake_site_analysis_and_events() -> None:
    llm = FakeLlm()
    page = FakeFetcher().fetch("https://acme.example/")
    analysis = llm.analyze_site([page]).value
    assert analysis.company_name == "Acme Analytics"
    events = llm.find_events(analysis, today=TODAY).value
    assert len(events) == 4
    starts = [e.start_date for e in events]
    assert any(s and s < TODAY for s in starts)
    assert any(s and s > TODAY for s in starts)
    assert all(e.source_url.startswith("https://") for e in events)


def test_fake_fetcher_fails_for_invalid_hosts() -> None:
    with pytest.raises(FetchError):
        FakeFetcher().fetch("https://down.invalid/")


def test_fake_search_is_deterministic_and_mixed() -> None:
    search = FakePostSearch(polls_until_done=2)
    run = search.start(['"RevOps Summit 2026" AND attending'], max_posts_per_query=50, max_charge_usd=Decimal(1))
    assert search.get(run.id).status == SearchStatus.RUNNING
    done = search.get(run.id)
    assert done.status == SearchStatus.SUCCEEDED
    assert done.cost_usd == Decimal("0.018")
    posts = search.posts(done)
    assert [p.id for p in posts] == [p.id for p in search.posts(done)]
    assert {p.author_kind for p in posts} == {"person", "company"}
    assert all("RevOps Summit 2026" in p.text for p in posts)


def test_fake_search_can_fail_like_a_real_run() -> None:
    search = FakePostSearch(failing_queries={"#Broken"})
    run = search.start(["#Broken"], max_posts_per_query=10, max_charge_usd=Decimal(1))
    assert search.get(run.id).status == SearchStatus.FAILED


def test_fake_labels_cover_the_main_cases() -> None:
    posts = [
        PostToLabel(post_id="1", text="Heading to RevOps Summit next week!"),
        PostToLabel(post_id="2", text="Back from RevOps Summit with takeaways"),
        PostToLabel(post_id="3", text="Visit us at booth 42"),
        PostToLabel(post_id="4", text="Early-bird tickets end Friday"),
        PostToLabel(post_id="5", text="Can't make it this year"),
        PostToLabel(post_id="6", text="Interesting industry"),
    ]
    labels = [p.label for p in FakeLlm().label_posts("RevOps Summit", posts).value]
    assert labels == [
        AttendanceLabel.ATTENDING,
        AttendanceLabel.ATTENDED,
        AttendanceLabel.EXHIBITING,
        AttendanceLabel.PROMOTING,
        AttendanceLabel.NOT_ATTENDING,
        AttendanceLabel.UNCLEAR,
    ]


def test_fake_enrichment_is_stable_and_keeps_the_batch_limit() -> None:
    enrich = FakeEnrichment()
    urls = [f"https://www.linkedin.com/in/person-{i}" for i in range(10)]
    first, second = enrich.match(urls), enrich.match(urls)
    assert first == second
    assert first.credits_used == sum(1 for p in first.people if p is not None)
    found = [p for p in first.people if p]
    assert all(p.email and p.email.endswith("@example.com") for p in found)
    with pytest.raises(ValueError, match="at most 10"):
        enrich.match([*urls, "https://www.linkedin.com/in/one-more"])


def test_fake_verifier_and_mailer() -> None:
    assert FakeEmailVerifier().verify("bounce@example.com") == "invalid"
    assert FakeEmailVerifier().verify("ok@example.com") == "safe"
    mailer = FakeMailer()
    mailer.send(OutgoingEmail(to="a@example.com", subject="one", text="1"))
    mailer.send(OutgoingEmail(to="a@example.com", subject="two", text="2"))
    last = mailer.last_to("a@example.com")
    assert last is not None
    assert last.subject == "two"
    assert mailer.last_to("b@example.com") is None


def test_fake_search_respects_the_post_and_spend_caps() -> None:
    search = FakePostSearch(posts_per_query=9)
    run = search.start(['"Pipeline Week"', "#PipelineWeek"], max_posts_per_query=3, max_charge_usd=Decimal("0.01"))
    done = search.get(run.id)
    assert len(search.posts(done)) == 6
    assert done.cost_usd == Decimal("0.01")


def test_each_fake_satisfies_its_interface() -> None:
    """mypy checks the assignments below statically; isinstance checks the method names at runtime."""
    fetcher: WebFetcher = FakeFetcher()
    llm: Llm = FakeLlm()
    search: PostSearch = FakePostSearch()
    enrich: PeopleEnrichment = FakeEnrichment()
    verifier: EmailVerifier = FakeEmailVerifier()
    mailer: Mailer = FakeMailer()
    for fake, interface in [
        (fetcher, WebFetcher),
        (llm, Llm),
        (search, PostSearch),
        (enrich, PeopleEnrichment),
        (verifier, EmailVerifier),
        (mailer, Mailer),
    ]:
        assert isinstance(fake, interface)
