"""The ICP score: what each signal is worth, and the reasons that come with it."""

import pytest

from app.pipeline.scoring import Profile, fits_a_persona, score
from app.schemas import Icp

ICP = Icp.model_validate(
    {
        "summary": "Marketing leaders at mid-size software companies.",
        "industries": ["Software", "Financial services"],
        "company_sizes": ["51-200", "201-500"],
        "regions": ["United States"],
        "personas": [
            {"label": "Marketing leaders", "title_keywords": ["marketing", "demand gen"], "seniorities": ["vp"]},
            {"label": "Revenue operations", "title_keywords": ["revops", "revenue operations"], "seniorities": []},
        ],
        "exclude_title_keywords": ["recruiter"],
    }
)


def profile(
    title: str | None = "VP Marketing",
    seniority: str | None = "vp",
    industry: str | None = "computer software",
    employees: int | None = 120,
) -> Profile:
    return Profile(title=title, seniority=seniority, industry=industry, employees=employees)


def test_a_perfect_fit_scores_100_with_every_reason() -> None:
    result = score(profile(), ICP)
    assert result.points == 100
    assert result.reasons == [
        "title matches 'marketing'",
        "seniority is vp",
        "industry matches 'Software'",
        "company size is 51-200",
    ]


@pytest.mark.parametrize(
    ("person", "points"),
    [
        (profile(seniority="manager"), 75),  # not a seniority this persona lists
        (profile(industry="hospital & health care"), 80),
        (profile(employees=5000), 85),  # 1001-5000 isn't one of the ICP's bands
        (profile(industry=None, employees=None), 65),  # Apollo didn't know the company
        (profile(title="Head of RevOps", seniority="head"), 75),  # a persona with no seniorities: the title counts
    ],
)
def test_each_signal_counts_on_its_own(person: Profile, points: int) -> None:
    assert score(person, ICP).points == points


def test_seniority_only_counts_with_a_title_that_fits() -> None:
    result = score(profile(title="VP Engineering"), ICP)  # a VP, but not of anything the ICP wants
    assert result.points == 35
    assert result.reasons == ["industry matches 'Software'", "company size is 51-200"]


def test_the_persona_whose_seniority_fits_wins() -> None:
    both = Icp.model_validate(
        {
            **ICP.model_dump(),
            "personas": [
                {"label": "Managers", "title_keywords": ["marketing"], "seniorities": ["manager"]},
                {"label": "Leaders", "title_keywords": ["marketing"], "seniorities": ["vp"]},
            ],
        }
    )
    assert score(profile(), both).points == 100


@pytest.mark.parametrize(
    ("industry", "matches"),
    [
        ("computer software", True),
        ("financial services", True),
        ("information technology & services", False),  # shares only "services", too general a word
        ("Software Development", True),
        ("retail", False),
    ],
)
def test_industries_match_on_their_telling_words(industry: str, matches: bool) -> None:
    assert ("industry matches" in " ".join(score(profile(industry=industry), ICP).reasons)) is matches


def test_shorthands_and_word_stems_count_as_the_same_industry() -> None:
    saas = Icp.model_validate({**ICP.model_dump(), "industries": ["B2B SaaS", "Healthcare"]})
    assert score(profile(industry="computer software"), saas).reasons[2] == "industry matches 'B2B SaaS'"
    assert score(profile(industry="hospital & health care"), saas).reasons[2] == "industry matches 'Healthcare'"


def test_a_headline_standing_in_for_the_title_says_so() -> None:
    result = score(Profile("Demand Gen @ Northwind", None, None, None, title_from_headline=True), ICP)
    assert (result.points, result.reasons) == (40, ["headline matches 'demand gen'"])


def test_nothing_known_scores_nothing() -> None:
    assert score(Profile(None, None, None, None), ICP).points == 0


@pytest.mark.parametrize(
    ("text", "fits"),
    [("Head of Demand Gen at Northwind", True), ("Software Engineer", False), (None, False), ("", False)],
)
def test_a_headline_hints_whether_someone_fits_before_apollo_is_asked(text: str | None, fits: bool) -> None:
    assert fits_a_persona(text, ICP) is fits
