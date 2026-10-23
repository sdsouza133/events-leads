"""The interfaces the pipeline depends on (ADR 0003). Each has a real implementation and a fake."""

from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.providers.types import FetchedPage, MatchResult, OutgoingEmail, SearchedPost, SearchRun
from app.schemas import EventSuggestion, LabeledPost, PostToLabel, SiteAnalysis


@runtime_checkable
class WebFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage:
        """Fetch one page. Raises FetchError (with a message safe to show the requester) when it can't."""
        ...


@runtime_checkable
class Llm(Protocol):
    def analyze_site(self, pages: list[FetchedPage]) -> SiteAnalysis: ...

    def find_events(self, analysis: SiteAnalysis, *, today: date) -> list[EventSuggestion]:
        """Events from live web search, never from memory: each one names the search result it came from."""
        ...

    def label_posts(self, event_name: str, posts: list[PostToLabel]) -> list[LabeledPost]: ...


@runtime_checkable
class PostSearch(Protocol):
    def start(self, queries: list[str], *, max_posts_per_query: int, max_charge_usd: Decimal) -> SearchRun: ...

    def get(self, run_id: str) -> SearchRun: ...

    def posts(self, run: SearchRun) -> list[SearchedPost]: ...


@runtime_checkable
class PeopleEnrichment(Protocol):
    def match(self, linkedin_urls: list[str]) -> MatchResult:
        """Look up to 10 people by LinkedIn URL."""
        ...


@runtime_checkable
class EmailVerifier(Protocol):
    def verify(self, email: str) -> str:
        """The verifier's status for the address, e.g. 'safe', 'invalid', 'catch_all'."""
        ...


@runtime_checkable
class Mailer(Protocol):
    def send(self, email: OutgoingEmail) -> None: ...
