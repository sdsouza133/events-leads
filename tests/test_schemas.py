import pytest
from pydantic import ValidationError

from app.schemas import EventSuggestion, Icp, Persona, SizeBand, size_band


@pytest.mark.parametrize(
    ("employees", "band"),
    [
        (1, SizeBand.S1_10),
        (10, SizeBand.S1_10),
        (11, SizeBand.S11_50),
        (200, SizeBand.S51_200),
        (1000, SizeBand.S501_1000),
        (10000, SizeBand.S5001_10000),
        (250000, SizeBand.S10001_PLUS),
    ],
)
def test_size_band(employees: int, band: SizeBand) -> None:
    assert size_band(employees) == band


def _event(**overrides: object) -> EventSuggestion:
    data: dict[str, object] = {
        "name": "RevOps Summit 2026",
        "url": "https://revops.example/",
        "source_url": "https://news.example/revops",
        "why": "RevOps leaders go.",
    }
    data.update(overrides)
    return EventSuggestion.model_validate(data)


def test_hashtags_are_stored_without_the_hash() -> None:
    assert _event(hashtags=["#RevOps2026", "RevOps", "#"]).hashtags == ["RevOps2026", "RevOps"]


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x.example/", "not a url"])
def test_event_links_must_be_web_addresses(bad: str) -> None:
    with pytest.raises(ValidationError):
        _event(url=bad)


def test_model_output_with_unexpected_fields_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _event(tickets_price="$999")


def test_an_icp_needs_at_least_one_persona_with_title_words() -> None:
    with pytest.raises(ValidationError):
        Icp(
            summary="x",
            industries=["Software"],
            company_sizes=[SizeBand.S51_200],
            regions=["Canada"],
            personas=[],
        )
    with pytest.raises(ValidationError):
        Persona(label="Marketing", title_keywords=[])
