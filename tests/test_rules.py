"""The free text rules: real-world phrasings (synthetic text), and the cases they must leave to Claude."""

import pytest

from app.pipeline.rules import label, prescreen
from app.schemas import AttendanceLabel, Icp

L = AttendanceLabel


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # attending
        ("Heading to SaaStr Annual next week. Who else is going?", L.ATTENDING),
        ("See you at #INBOUND26! DM me if you want to grab a coffee.", L.ATTENDING),
        ("I'll be at Pipeline Summit on Thursday, let's connect.", L.ATTENDING),
        ("Excited to attend RevOps Forum in Austin this October.", L.ATTENDING),
        ("Can't wait to be at Dreamforce, my first one!", L.ATTENDING),
        ("I'm in town for Web Summit, let's meet at the Startup Stage.", L.ATTENDING),
        # attended
        ("Back from SaaStr Annual with a notebook full of ideas.", L.ATTENDED),
        ("My key takeaways from Pipeline Summit 2026:", L.ATTENDED),
        ("What a week at INBOUND. Thank you to everyone I met at the booth crawl.", L.ATTENDED),
        ("Recap of RevOps Forum: three things that stuck with me.", L.ATTENDED),
        ("Just back from Dreamforce and still processing.", L.ATTENDED),
        # speaking
        ("I'm speaking at SaaStr Annual on Wednesday about pricing.", L.SPEAKING),
        ("I'll be on the panel at Pipeline Summit: the future of outbound.", L.SPEAKING),
        ("Catch me on stage at RevOps Forum, 2pm, main hall.", L.SPEAKING),
        ("Slides from my talk at INBOUND are up.", L.SPEAKING),
        # exhibiting
        ("Visit us at booth 42 at SaaStr Annual for a live demo.", L.EXHIBITING),
        ("Stop by our team at the booth? Come see us at booth #7!", L.EXHIBITING),
        ("Find us at booth no. 118 all week.", L.EXHIBITING),
        # organizer
        ("We're hosting Pipeline Summit 2026 and the agenda is live!", L.ORGANIZER),
        ("Proud to organize RevOps Forum for the third year.", L.ORGANIZER),
        # promoting
        ("Use my code SPEAKER20 for 20% off SaaStr Annual tickets.", L.PROMOTING),
        ("Early-bird tickets for Pipeline Summit end Friday. Register now.", L.PROMOTING),
        ("Registration is now open for RevOps Forum.", L.PROMOTING),
        # not attending
        ("Can't make it to SaaStr this year. Send me your notes!", L.NOT_ATTENDING),
        ("I'm not going to INBOUND, but I'll be following along online.", L.NOT_ATTENDING),
        ("Missing it this year, watching the livestream instead.", L.NOT_ATTENDING),
    ],
)
def test_clear_phrasings_get_their_label(text: str, expected: AttendanceLabel) -> None:
    assert label(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Heading to SaaStr, and catch me on stage on day two!", L.SPEAKING),
        ("Heading to INBOUND, visit us at booth 12.", L.EXHIBITING),
        ("Back from Dreamforce. I'll be at Web Summit next month too.", L.ATTENDED),
    ],
)
def test_several_signs_of_being_there_keep_the_most_specific(text: str, expected: AttendanceLabel) -> None:
    assert label(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Can't make it to SaaStr, but my team is at booth 12.",  # not going, yet the company exhibits
        "Can\u2019t make it to SaaStr, but my team is at booth 12.",  # the same, typed with a curly apostrophe
        "Use code EARLY for 15% off. See you at Pipeline Summit!",  # selling tickets and going?
        "Great lineup announced for RevOps Forum 2026.",  # mentions it, says nothing about the author
        "Is anyone going to INBOUND this year?",
        "",
    ],
)
def test_unclear_or_conflicting_posts_are_left_to_claude(text: str) -> None:
    assert label(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I\u2019ll be at Pipeline Summit on Thursday.", L.ATTENDING),  # the typographic apostrophe
        ("I\u2018m speaking at SaaStr Annual on Wednesday.", L.SPEAKING),  # its opening twin, a common slip
        ("Can\u02bct make it to SaaStr this year.", L.NOT_ATTENDING),  # the modifier letter some keyboards type
        ("We\u2019re hosting Pipeline Summit 2026.", L.ORGANIZER),
    ],
)
def test_curly_apostrophes_read_like_straight_ones(text: str, expected: AttendanceLabel) -> None:
    assert label(text) == expected


def test_a_negation_does_not_read_as_its_opposite() -> None:
    assert label("I'm not speaking at SaaStr this time.") is None
    assert label("Internal kickoff after Pipeline Summit") is None  # no rule matches "Internal"


ICP = Icp.model_validate(
    {
        "summary": "Marketing leaders at mid-size SaaS companies.",
        "industries": ["Software"],
        "company_sizes": ["51-200"],
        "regions": ["United States"],
        "personas": [{"label": "Marketing", "title_keywords": ["marketing"], "seniorities": ["vp"]}],
        "exclude_title_keywords": ["recruiter", "account executive"],
    }
)


@pytest.mark.parametrize(
    ("headline", "reason"),
    [
        ("MBA Student at Wharton | Marketing", "headline says 'student'"),
        ("Marketing Intern @ Bluefin", "headline says 'intern'"),
        ("Technical Recruiter | Hiring engineers", "headline says 'recruiter'"),
        ("Senior Account  Executive at Copperline", "headline says 'account executive'"),
        ("Former VP Marketing | Open to work", "headline says 'open to work'"),
        ("VP Marketing at Acme Analytics", "works at the requester's company"),
    ],
)
def test_headlines_that_cannot_match_are_screened_out(headline: str, reason: str) -> None:
    assert prescreen(headline, ICP, own_company="Acme Analytics") == reason


@pytest.mark.parametrize(
    "headline",
    [
        "VP Marketing at Northwind Labs",
        "Head of Internal Communications",  # "intern" only as part of a word
        "Founder | Helping B2B teams grow",
        None,
        "",
    ],
)
def test_other_headlines_are_left_for_apollo_to_judge(headline: str | None) -> None:
    assert prescreen(headline, ICP, own_company="Acme Analytics") is None
