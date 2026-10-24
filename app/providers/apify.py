"""LinkedIn post search through Apify's `harvestapi/linkedin-post-search` actor (public posts, no login or cookies).

Pay per post. Every run is started with `maxTotalChargeUsd`, so Apify itself stops it at the amount we reserved.
"""

from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any

import httpx2

from app.linkedin import normalize_profile_url
from app.providers import http
from app.providers.types import SearchedPost, SearchRun, SearchStatus

ACTOR = "harvestapi~linkedin-post-search"
SERVICE = "Apify"
PAGE_SIZE = 1000
RUN_TIMEOUT_SECONDS = 900

_RUNNING = {"READY", "RUNNING"}
# A run stopped by its time or charge limit still returns (and charges for) what it found, so it counts as done.
_DONE = {"SUCCEEDED", "TIMED-OUT", "ABORTED"}


class ApifyPostSearch:
    def __init__(
        self,
        token: str,
        *,
        actor: str = ACTOR,
        posted_limit: str = "3months",
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._actor = actor
        self._posted_limit = posted_limit
        self._http = http.client(
            "https://api.apify.com/v2", headers={"Authorization": f"Bearer {token}"}, transport=transport
        )

    def start(self, queries: list[str], *, max_posts_per_query: int, max_charge_usd: Decimal) -> SearchRun:
        actor_input = {
            "searchQueries": queries,
            "maxPosts": max_posts_per_query,
            "postedLimit": self._posted_limit,
            "sortBy": "relevance",
            "profileScraperMode": "short",
            "scrapeReactions": False,
            "scrapeComments": False,
        }
        params = {"maxTotalChargeUsd": str(max_charge_usd), "timeout": str(RUN_TIMEOUT_SECONDS)}
        body = http.send(lambda: self._http.post(f"/acts/{self._actor}/runs", params=params, json=actor_input), SERVICE)
        return _run(body["data"])

    def get(self, run_id: str) -> SearchRun:
        return _run(http.send(lambda: self._http.get(f"/actor-runs/{run_id}"), SERVICE)["data"])

    def posts(self, run: SearchRun) -> list[SearchedPost]:
        if not run.dataset_id:
            return []
        out: list[SearchedPost] = []
        offset = 0
        while True:
            params = {"clean": "true", "format": "json", "limit": str(PAGE_SIZE), "offset": str(offset)}
            items = http.send(partial(self._http.get, f"/datasets/{run.dataset_id}/items", params=params), SERVICE)
            out.extend(p for p in (parse_post(i) for i in items) if p is not None)
            if len(items) < PAGE_SIZE:
                return out
            offset += PAGE_SIZE


def _run(data: dict[str, Any]) -> SearchRun:
    status = str(data.get("status") or "")
    usage = data.get("usageTotalUsd")
    cost = Decimal(str(usage)) if usage is not None else None
    if status in _RUNNING:
        mapped = SearchStatus.RUNNING
    elif status in _DONE:
        mapped = SearchStatus.SUCCEEDED
    else:
        mapped = SearchStatus.FAILED
    return SearchRun(
        id=str(data["id"]),
        status=mapped,
        dataset_id=data.get("defaultDatasetId"),
        cost_usd=cost,
        message=data.get("statusMessage"),
    )


def parse_post(item: dict[str, Any]) -> SearchedPost | None:
    """One dataset item to a post, or None when it has no id or no text (nothing to judge)."""
    post_id = str(item.get("id") or "")
    text = item.get("content") or (item.get("repost") or {}).get("content") or ""
    if not post_id or not text.strip():
        return None
    author = item.get("author") or {}
    is_company = author.get("type") == "company"
    posted = (item.get("postedAt") or {}).get("date")
    return SearchedPost(
        id=post_id,
        url=item.get("linkedinUrl") or "",
        text=text,
        author_name=author.get("name"),
        author_url=author.get("linkedinUrl") if is_company else normalize_profile_url(author.get("linkedinUrl")),
        author_headline=None if is_company else author.get("info"),
        author_kind="company" if is_company else "person",
        posted_at=datetime.fromisoformat(posted.replace("Z", "+00:00")) if posted else None,
        query=(item.get("query") or {}).get("search"),
    )
