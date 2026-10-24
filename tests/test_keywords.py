from datetime import date

import pytest

from app.pipeline.keywords import GOING, MAX_QUERIES, WENT, build_queries, hashtag, phrase
from app.schemas import EventSuggestion

TODAY = date(2026, 10, 24)


def event(**fields: object) -> EventSuggestion:
    base: dict[str, object] = {
        "name": "RevOps Summit 2026",
        "short_name": "RevOps Summit",
        "hashtags": ["RevOpsSummit2026", "RevOps"],
        "url": "https://revops.example/",
        "source_url": "https://news.example/revops",
        "why": "RevOps leaders go.",
    }
    return EventSuggestion.model_validate({**base, **fields})


def test_an_upcoming_event_searches_for_people_saying_they_will_be_there() -> None:
    queries = build_queries(event(start_date="2026-11-12", end_date="2026-11-13"), today=TODAY)
    assert queries == [
        '"RevOps Summit 2026"',
        "#RevOpsSummit2026",
        f'"RevOps Summit" AND {GOING}',
        '"RevOps Summit"',
    ]


def test_a_past_event_searches_for_recaps() -> None:
    queries = build_queries(event(start_date="2026-09-01", end_date="2026-09-02"), today=TODAY)
    assert f'"RevOps Summit" AND {WENT}' in queries
    assert not any(GOING in q for q in queries)


@pytest.mark.parametrize(
    "dates",
    [{"start_date": "2026-10-23", "end_date": "2026-10-25"}, {}],  # happening now, or dates unknown
)
def test_an_ongoing_or_undated_event_searches_both_ways(dates: dict[str, str]) -> None:
    queries = build_queries(event(**dates), today=TODAY)
    assert any(GOING in q for q in queries)
    assert any(WENT in q for q in queries)
    assert len(queries) == MAX_QUERIES


def test_without_a_short_name_the_full_name_is_used_everywhere() -> None:
    queries = build_queries(event(short_name=None, hashtags=[], start_date="2026-12-01"), today=TODAY)
    assert queries == ['"RevOps Summit 2026"', f'"RevOps Summit 2026" AND {GOING}']


def test_a_short_name_equal_to_the_full_name_is_not_searched_twice() -> None:
    queries = build_queries(event(short_name="revops summit 2026", start_date="2026-12-01"), today=TODAY)
    assert len(queries) == len({q.lower() for q in queries})


def test_quotes_and_punctuation_cannot_break_a_search() -> None:
    assert phrase('The "Big" Show') == '"The Big Show"'
    assert phrase('""') == ""
    assert hashtag("#Saa-S Fest!") == "#SaaSFest"
    assert hashtag("!!!") == ""
