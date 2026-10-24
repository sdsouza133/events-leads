"""The domain's shared shapes: the ICP, events and attendance labels.

They are Pydantic models because they cross trust boundaries: Claude produces them (and its output is validated
against these exact schemas), requesters edit them in the plan form, and they are stored as JSON on the run.
"""

import enum
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Sentence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=2048, pattern=r"^https?://")]
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=300)]
"""One LinkedIn post search, e.g. `"SaaStr Annual" AND (attending OR speaking)`."""

MAX_QUERIES = 4
"""LinkedIn searches per event."""


class Seniority(enum.StrEnum):
    """Apollo's seniority values, so ICP checks compare like with like."""

    OWNER = "owner"
    FOUNDER = "founder"
    C_SUITE = "c_suite"
    PARTNER = "partner"
    VP = "vp"
    HEAD = "head"
    DIRECTOR = "director"
    MANAGER = "manager"
    SENIOR = "senior"
    ENTRY = "entry"


class SizeBand(enum.StrEnum):
    S1_10 = "1-10"
    S11_50 = "11-50"
    S51_200 = "51-200"
    S201_500 = "201-500"
    S501_1000 = "501-1000"
    S1001_5000 = "1001-5000"
    S5001_10000 = "5001-10000"
    S10001_PLUS = "10001+"


_BAND_UPPER_BOUNDS = [
    (10, SizeBand.S1_10),
    (50, SizeBand.S11_50),
    (200, SizeBand.S51_200),
    (500, SizeBand.S201_500),
    (1000, SizeBand.S501_1000),
    (5000, SizeBand.S1001_5000),
    (10000, SizeBand.S5001_10000),
]


def size_band(employees: int) -> SizeBand:
    for upper, band in _BAND_UPPER_BOUNDS:
        if employees <= upper:
            return band
    return SizeBand.S10001_PLUS


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Persona(_Strict):
    label: ShortText = Field(description="Who this is, e.g. 'Demand generation leaders'")
    title_keywords: list[ShortText] = Field(
        min_length=1, max_length=15, description="Words that appear in their job titles, e.g. 'demand gen', 'growth'"
    )
    seniorities: list[Seniority] = Field(default_factory=list, max_length=10)


class Icp(_Strict):
    summary: Sentence
    industries: list[ShortText] = Field(min_length=1, max_length=12)
    company_sizes: list[SizeBand] = Field(min_length=1, max_length=8)
    regions: list[ShortText] = Field(min_length=1, max_length=12, description="Countries or regions")
    personas: list[Persona] = Field(min_length=1, max_length=5)
    exclude_title_keywords: list[ShortText] = Field(default_factory=list, max_length=20)


class SiteAnalysis(_Strict):
    company_name: ShortText
    offering: Sentence = Field(description="What the company sells, in one or two sentences")
    icp: Icp
    event_topics: list[ShortText] = Field(min_length=1, max_length=8, description="Themes to look for events about")


class EventFields(_Strict):
    """What we know of any event: one we suggested, one the requester added, or one they approved."""

    name: ShortText
    short_name: ShortText | None = None
    hashtags: list[ShortText] = Field(default_factory=list, max_length=5)
    start_date: date | None = None
    end_date: date | None = None
    location: ShortText | None = Field(default=None, description="City and country, or 'Online'")

    @field_validator("hashtags")
    @classmethod
    def _bare_hashtags(cls, tags: list[str]) -> list[str]:
        return [t.lstrip("#") for t in tags if t.lstrip("#")]


class EventSuggestion(EventFields):
    url: Url
    source_url: Url = Field(description="The web search result this event was found in")
    why: Sentence


class ApprovedEvent(EventFields):
    """An event the requester approved as suggested, edited, or added, with the searches to run for it."""

    url: Url | None = None
    queries: list[Query] = Field(min_length=1, max_length=MAX_QUERIES)
    source: Literal["suggested", "requester"] = "suggested"


class AttendanceLabel(enum.StrEnum):
    ATTENDING = "attending"
    ATTENDED = "attended"
    SPEAKING = "speaking"
    EXHIBITING = "exhibiting"
    ORGANIZER = "organizer"
    PROMOTING = "promoting"
    NOT_ATTENDING = "not_attending"
    UNCLEAR = "unclear"


QUALIFYING_LABELS = frozenset(
    {AttendanceLabel.ATTENDING, AttendanceLabel.ATTENDED, AttendanceLabel.SPEAKING, AttendanceLabel.EXHIBITING}
)
"""Labels that mean the author is (or was) really there."""

MOST_TELLING = [
    AttendanceLabel.SPEAKING,
    AttendanceLabel.EXHIBITING,
    AttendanceLabel.ATTENDING,
    AttendanceLabel.ATTENDED,
]
"""The qualifying labels, best lead first: a speaker or exhibitor is easiest to approach, then those still going."""


class PostToLabel(_Strict):
    post_id: str
    author_headline: str | None = None
    text: str


class LabeledPost(_Strict):
    post_id: str
    label: AttendanceLabel
    confidence: float = Field(ge=0, le=1)
    reason: Annotated[str, StringConstraints(max_length=300)]
