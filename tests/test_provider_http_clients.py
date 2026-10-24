"""Contract tests for the Apify, Apollo, Reoon and Resend clients, against synthetic responses shaped like the real
ones (field names and structure copied from live responses; every value is made up)."""

import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from app.providers.apify import ApifyPostSearch, parse_post
from app.providers.apollo import ApolloEnrichment
from app.providers.reoon import ReoonVerifier
from app.providers.resend import ResendMailer
from app.providers.types import OutgoingEmail, ProviderError, SearchRun, SearchStatus


class Recorder:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.requests: list[httpx2.Request] = []
        self._routes = routes

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self._routes[request.url.path]
        if callable(answer):
            answer = answer(request)
        return answer if isinstance(answer, httpx2.Response) else httpx2.Response(200, json=answer)


# ------------------------------------------------------------------------------------------------------- Apify
POST_ITEM = {
    "type": "post",
    "id": "7390000000000000001",
    "linkedinUrl": "https://www.linkedin.com/posts/avery-okafor_revops-activity-7390000000000000001-abcd",
    "content": "Heading to RevOps Summit next week!",
    "author": {
        "type": "profile",
        "name": "Avery Okafor",
        "linkedinUrl": "https://www.linkedin.com/in/Avery-Okafor-1234?miniProfileUrn=urn%3Ali%3Afs",
        "info": "Head of Growth at Northwind Labs",
        "publicIdentifier": "avery-okafor-1234",
    },
    "postedAt": {"timestamp": 1792611808091, "date": "2026-10-21T19:43:28.091Z", "postedAgoShort": "1d"},
    "query": {"sortBy": "relevance", "page": 1, "search": '"RevOps Summit 2026" AND attending'},
}


def run_data(status: str, **extra: object) -> dict[str, Any]:
    return {"data": {"id": "run123", "status": status, "defaultDatasetId": "ds456", **extra}}


def test_apify_start_caps_the_charge_and_reads_no_profiles() -> None:
    routes = {"/v2/acts/harvestapi~linkedin-post-search/runs": run_data("READY")}
    rec = Recorder(routes)
    run = ApifyPostSearch("tok", transport=httpx2.MockTransport(rec)).start(
        ['"RevOps Summit 2026"'], max_posts_per_query=50, max_charge_usd=Decimal("0.40")
    )
    assert (run.id, run.status) == ("run123", SearchStatus.RUNNING)
    request = rec.requests[0]
    assert request.url.params["maxTotalChargeUsd"] == "0.40"
    assert request.headers["authorization"] == "Bearer tok"
    body = json.loads(request.content)
    assert body["searchQueries"] == ['"RevOps Summit 2026"']
    assert body["maxPosts"] == 50
    assert body["profileScraperMode"] == "short"  # full profiles cost extra
    assert body["scrapeReactions"] is body["scrapeComments"] is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("RUNNING", SearchStatus.RUNNING),
        ("SUCCEEDED", SearchStatus.SUCCEEDED),
        ("TIMED-OUT", SearchStatus.SUCCEEDED),  # partial results are still charged and still useful
        ("ABORTED", SearchStatus.SUCCEEDED),
        ("FAILED", SearchStatus.FAILED),
    ],
)
def test_apify_run_statuses(status: str, expected: SearchStatus) -> None:
    rec = Recorder({"/v2/actor-runs/run123": run_data(status, usageTotalUsd=0.10005)})
    run = ApifyPostSearch("tok", transport=httpx2.MockTransport(rec)).get("run123")
    assert run.status == expected
    assert run.cost_usd == Decimal("0.10005")


def test_apify_posts_are_paged_and_parsed() -> None:
    first = [dict(POST_ITEM, id=str(i)) for i in range(1000)]
    second = [dict(POST_ITEM, id="last"), {"id": "", "content": "no id"}, dict(POST_ITEM, id="empty", content="")]
    pages = iter([first, second])
    rec = Recorder({"/v2/datasets/ds456/items": lambda _req: httpx2.Response(200, json=next(pages))})
    search = ApifyPostSearch("tok", transport=httpx2.MockTransport(rec))
    posts = search.posts(SearchRun(id="run123", status=SearchStatus.SUCCEEDED, dataset_id="ds456"))
    assert len(posts) == 1001
    assert [r.url.params["offset"] for r in rec.requests] == ["0", "1000"]


def test_a_post_item_becomes_a_normalized_post() -> None:
    post = parse_post(POST_ITEM)
    assert post is not None
    assert post.author_url == "https://www.linkedin.com/in/avery-okafor-1234"
    assert post.author_headline == "Head of Growth at Northwind Labs"
    assert post.author_kind == "person"
    assert post.posted_at is not None
    assert post.posted_at.isoformat() == "2026-10-21T19:43:28.091000+00:00"
    assert post.query == '"RevOps Summit 2026" AND attending'


def test_company_posts_and_reposts() -> None:
    company = dict(
        POST_ITEM,
        author={"type": "company", "name": "Northwind", "linkedinUrl": "https://www.linkedin.com/company/northwind"},
    )
    parsed = parse_post(company)
    assert parsed is not None
    assert (parsed.author_kind, parsed.author_headline) == ("company", None)
    repost = dict(POST_ITEM, content="", repost={"content": "Original text about RevOps Summit"})
    reposted = parse_post(repost)
    assert reposted is not None
    assert reposted.text == "Original text about RevOps Summit"


def test_apify_errors_are_classified() -> None:
    rec = Recorder({"/v2/actor-runs/run123": httpx2.Response(401, json={"error": {"type": "token-not-valid"}})})
    with pytest.raises(ProviderError) as exc:
        ApifyPostSearch("bad", transport=httpx2.MockTransport(rec)).get("run123")
    assert not exc.value.retryable


# ------------------------------------------------------------------------------------------------------ Apollo
APOLLO_PERSON = {
    "id": "6600000000000000000000aa",
    "first_name": "Avery",
    "last_name": "Okafor",
    "name": "Avery Okafor",
    "linkedin_url": "http://www.linkedin.com/in/avery-okafor-1234",
    "title": "Head of Growth",
    "headline": "Head of Growth at Northwind Labs",
    "email": "Avery.Okafor@northwind.example",
    "email_status": "verified",
    "email_domain_catchall": False,
    "seniority": "head",
    "departments": ["master_marketing"],
    "country": "Canada",
    "organization": {
        "name": "Northwind Labs",
        "primary_domain": "northwind.example",
        "industry": "computer software",
        "estimated_num_employees": 180,
    },
}


def test_apollo_bulk_match_keeps_the_order_asked() -> None:
    reply = {"status": "success", "credits_consumed": 1, "matches": [None, APOLLO_PERSON]}
    rec = Recorder({"/api/v1/people/bulk_match": reply})
    urls = ["https://www.linkedin.com/in/nobody", "https://www.linkedin.com/in/avery-okafor-1234"]
    result = ApolloEnrichment("key", transport=httpx2.MockTransport(rec)).match(urls)
    assert result.credits_used == 1
    assert result.people[0] is None
    person = result.people[1]
    assert person is not None
    assert person.email == "avery.okafor@northwind.example"
    assert (person.email_status, person.seniority, person.company_size) == ("verified", "head", 180)
    assert person.linkedin_url == "https://www.linkedin.com/in/avery-okafor-1234"
    request = rec.requests[0]
    assert request.headers["x-api-key"] == "key"
    body = json.loads(request.content)
    assert body["reveal_personal_emails"] is False
    assert body["details"] == [{"linkedin_url": u} for u in urls]


def test_apollo_refuses_more_than_ten_and_skips_empty_batches() -> None:
    enrich = ApolloEnrichment("key", transport=httpx2.MockTransport(Recorder({})))
    with pytest.raises(ValueError, match="at most 10"):
        enrich.match([f"https://www.linkedin.com/in/p{i}" for i in range(11)])
    assert enrich.match([]).credits_used == 0


def test_apollo_rate_limits_are_retryable() -> None:
    rec = Recorder({"/api/v1/people/bulk_match": httpx2.Response(429, json={"error": "rate limited"})})
    with pytest.raises(ProviderError) as exc:
        ApolloEnrichment("key", transport=httpx2.MockTransport(rec)).match(["https://www.linkedin.com/in/x"])
    assert exc.value.retryable


# --------------------------------------------------------------------------------------------- Reoon and Resend
def test_reoon_returns_the_status() -> None:
    rec = Recorder({"/api/v1/verify": {"email": "a@northwind.example", "status": "Safe"}})
    assert ReoonVerifier("rkey", transport=httpx2.MockTransport(rec)).verify("a@northwind.example") == "safe"
    assert rec.requests[0].url.params["mode"] == "power"


def test_network_failures_are_retryable() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    with pytest.raises(ProviderError) as exc:
        ReoonVerifier("rkey", transport=httpx2.MockTransport(broken)).verify("a@x.example")
    assert exc.value.retryable
    assert exc.value.cost_usd is None  # the request may have got through: what it cost is unknown
    assert "rkey" not in str(exc.value)  # the key never leaks into error messages


@pytest.mark.parametrize(("status", "retryable"), [(408, True), (425, True), (429, True), (503, True), (422, False)])
def test_which_statuses_are_worth_retrying(status: int, retryable: bool) -> None:
    rec = Recorder({"/api/v1/verify": httpx2.Response(status, json={"error": "x"})})
    with pytest.raises(ProviderError) as exc:
        ReoonVerifier("rkey", transport=httpx2.MockTransport(rec)).verify("a@x.example")
    assert exc.value.retryable is retryable
    assert exc.value.cost_usd == 0  # the service answered with an error: nothing was charged


def test_resend_sends_with_an_idempotency_key() -> None:
    rec = Recorder({"/emails": {"id": "email_1"}})
    mailer = ResendMailer("rs_key", sender="Event Leads <leads@example.com>", transport=httpx2.MockTransport(rec))
    mailer.send(OutgoingEmail(to="jo@acme.example", subject="Your list", text="hi", html="<p>hi</p>", key="results:r1"))
    request = rec.requests[0]
    assert request.headers["idempotency-key"] == "results:r1"
    assert request.headers["authorization"] == "Bearer rs_key"
    assert json.loads(request.content) == {
        "from": "Event Leads <leads@example.com>",
        "to": ["jo@acme.example"],
        "subject": "Your list",
        "text": "hi",
        "html": "<p>hi</p>",
    }


def test_unreadable_responses_are_retryable() -> None:
    rec = Recorder({"/emails": httpx2.Response(200, content=b"<html>gateway</html>")})
    mailer = ResendMailer("k", sender="x@example.com", transport=httpx2.MockTransport(rec))
    with pytest.raises(ProviderError) as exc:
        mailer.send(OutgoingEmail(to="a@b.example", subject="s", text="t", key="signin:1"))
    assert exc.value.retryable


@pytest.mark.parametrize(
    ("name", "retryable"),
    [
        ("concurrent_idempotent_requests", True),  # the first send with this key is still in progress
        ("invalid_idempotent_request", False),  # the same key with a different email: our bug, retrying won't help
    ],
)
def test_resend_conflicts_are_retried_only_while_the_first_send_is_running(name: str, retryable: bool) -> None:
    conflict = {"statusCode": 409, "name": name, "message": "x"}
    rec = Recorder({"/emails": httpx2.Response(409, json=conflict)})
    mailer = ResendMailer("k", sender="x@example.com", transport=httpx2.MockTransport(rec))
    with pytest.raises(ProviderError) as exc:
        mailer.send(OutgoingEmail(to="a@b.example", subject="s", text="t", key="results:r1"))
    assert exc.value.retryable is retryable
