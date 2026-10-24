"""The ICP score (issue #7): how well a person Apollo found fits the requester's ICP, and why.

| Signal | Points | Counts when |
|---|---|---|
| Title | 40 | the title has one of a persona's title keywords |
| Seniority | 25 | the title matched, and the seniority is one that persona lists |
| Industry | 20 | the company's industry shares a word with one of the ICP's industries |
| Company size | 15 | the company's headcount is in one of the ICP's size bands |

Seniority counts only for the persona the title matched: a VP of Engineering doesn't fit an ICP of marketing
leaders. With the default MIN_ICP_SCORE of 40, nobody makes the list without a title that fits a persona.
"""

import re
from dataclasses import dataclass, field

from app.pipeline.rules import mentions
from app.schemas import Icp, Persona, size_band

TITLE_POINTS = 40
SENIORITY_POINTS = 25
INDUSTRY_POINTS = 20
SIZE_POINTS = 15

# Words too general to say two industries are the same ("Financial services" isn't "IT services").
_GENERIC = frozenset(
    {
        "and",
        "the",
        "for",
        "services",
        "service",
        "solutions",
        "technology",
        "technologies",
        "industry",
        "industries",
        "company",
        "companies",
        "products",
        "management",
        "other",
    }
)
# Shorthands an ICP uses for what Apollo spells out.
_ALIASES = {
    "saas": "software",
    "fintech": "financial",
    "martech": "marketing",
    "adtech": "advertising",
    "edtech": "education",
    "healthtech": "health",
}
_MIN_WORD = 3
_MIN_STEM = 4  # "health" matches "healthcare", but "law" doesn't match "lawn"


@dataclass(frozen=True)
class Profile:
    """What Apollo knows about a person, as far as the ICP goes."""

    title: str | None
    seniority: str | None
    industry: str | None
    employees: int | None
    title_from_headline: bool = False  # Apollo had no title, so the LinkedIn headline stands in for it


@dataclass(frozen=True)
class Score:
    points: int
    reasons: list[str] = field(default_factory=list)


def score(profile: Profile, icp: Icp) -> Score:
    points, reasons = 0, []
    fit = _persona_fit(profile, icp.personas)
    if fit is not None:
        keyword, seniority_fits = fit
        points += TITLE_POINTS
        reasons.append(f"{'headline' if profile.title_from_headline else 'title'} matches {keyword!r}")
        if seniority_fits:
            points += SENIORITY_POINTS
            reasons.append(f"seniority is {profile.seniority}")
    industry = _matching_industry(profile.industry, icp.industries)
    if industry is not None:
        points += INDUSTRY_POINTS
        reasons.append(f"industry matches {industry!r}")
    if profile.employees is not None and (band := size_band(profile.employees)) in icp.company_sizes:
        points += SIZE_POINTS
        reasons.append(f"company size is {band}")
    return Score(points, reasons)


def fits_a_persona(text: str | None, icp: Icp) -> bool:
    """Whether a title or headline has any persona's title keyword (the free hint used before Apollo is asked)."""
    return bool(text) and any(_keyword_in(text, persona) for persona in icp.personas)


def _persona_fit(profile: Profile, personas: list[Persona]) -> tuple[str, bool] | None:
    """The matched title keyword and whether the seniority fits too, preferring a persona where both do."""
    best: tuple[str, bool] | None = None
    for persona in personas:
        keyword = _keyword_in(profile.title, persona)
        if keyword is None:
            continue
        seniority_fits = profile.seniority is not None and profile.seniority in persona.seniorities
        if best is None or (seniority_fits and not best[1]):
            best = (keyword, seniority_fits)
    return best


def _keyword_in(title: str | None, persona: Persona) -> str | None:
    if not title:
        return None
    return next((keyword for keyword in persona.title_keywords if mentions(title, keyword)), None)


def _matching_industry(industry: str | None, wanted: list[str]) -> str | None:
    if not industry:
        return None
    have = _words(industry)
    return next((name for name in wanted if any(_same(a, b) for a in _words(name) for b in have)), None)


def _words(text: str) -> set[str]:
    words = {_ALIASES.get(word, word) for word in re.findall(r"[a-z0-9]+", text.casefold())}
    return {word for word in words if len(word) >= _MIN_WORD and word not in _GENERIC}


def _same(a: str, b: str) -> bool:
    short, long = sorted((a, b), key=len)
    return a == b or (len(short) >= _MIN_STEM and long.startswith(short))
