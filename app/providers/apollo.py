"""People enrichment through Apollo's bulk match: up to 10 LinkedIn URLs per call, one credit per person found."""

from typing import Any

import httpx2

from app.linkedin import normalize_profile_url
from app.providers import http
from app.providers.types import EnrichedPerson, MatchResult

SERVICE = "Apollo"
BATCH_LIMIT = 10


class ApolloEnrichment:
    def __init__(self, api_key: str, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._http = http.client(
            "https://api.apollo.io/api/v1",
            headers={"X-Api-Key": api_key, "Cache-Control": "no-cache", "Accept": "application/json"},
            transport=transport,
        )

    def match(self, linkedin_urls: list[str]) -> MatchResult:
        if len(linkedin_urls) > BATCH_LIMIT:
            raise ValueError(f"at most {BATCH_LIMIT} people per call")
        if not linkedin_urls:
            return MatchResult(people=[], credits_used=0)
        body = {
            "details": [{"linkedin_url": url} for url in linkedin_urls],
            "reveal_personal_emails": False,  # work emails only
            "reveal_phone_number": False,
        }
        data = http.send(lambda: self._http.post("/people/bulk_match", json=body), SERVICE)
        matches = data.get("matches") or []
        # Matches come back in the order asked, with null where nobody was found.
        people = [
            _person(url, matches[i]) if i < len(matches) and matches[i] else None for i, url in enumerate(linkedin_urls)
        ]
        return MatchResult(people=people, credits_used=int(data.get("credits_consumed") or 0))


def _person(asked_url: str, m: dict[str, Any]) -> EnrichedPerson:
    org = m.get("organization") or {}
    employees = org.get("estimated_num_employees")
    return EnrichedPerson(
        linkedin_url=normalize_profile_url(asked_url) or asked_url,
        apollo_id=m.get("id"),
        name=m.get("name"),
        headline=m.get("headline"),
        title=m.get("title"),
        seniority=m.get("seniority"),
        departments=list(m.get("departments") or []),
        email=(m.get("email") or "").strip().lower() or None,
        email_status=m.get("email_status"),
        email_domain_catchall=bool(m.get("email_domain_catchall")),
        company_name=org.get("name"),
        company_domain=org.get("primary_domain"),
        company_industry=org.get("industry"),
        company_size=int(employees) if isinstance(employees, int | float) else None,
        country=m.get("country") or org.get("country"),
    )
