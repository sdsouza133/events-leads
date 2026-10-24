"""Text rules that label the obvious posts for free (issue #6). Anything they aren't sure of goes to Claude.

Each rule is a phrase that, in a post found by searching for the event, settles the question on its own ("back from",
"visit us at booth 12", "use my discount code"). A post can match several rules: when they all mean the author is
there ("heading to ..., catch me on stage"), the most specific wins; when they disagree ("can't make it, but my team
is at booth 12"), the post goes to Claude, and so does a post that matches none.

The labels mean what they mean in Claude's instructions (`app/providers/anthropic_llm.py`, LABEL_SYSTEM).
"""

import re

from app.schemas import QUALIFYING_LABELS, AttendanceLabel, Icp

RULE_CONFIDENCE = 0.9

_I = r"(?:i'?m|i am|i'?ll be|i will be)"

_RULES: list[tuple[AttendanceLabel, re.Pattern[str]]] = [
    (
        AttendanceLabel.NOT_ATTENDING,
        re.compile(
            r"\b(?:can'?t|cannot|won'?t|unable to) (?:make it|attend|be there|go)\b"
            r"|\b(?:i'?m|i am) not (?:going|attending|making it)\b"
            r"|\bmissing (?:it|out on it) this year\b"
            r"|\b(?:following|watching) (?:it |along )?(?:online|remotely|the (?:live ?)?stream)\b",
            re.IGNORECASE,
        ),
    ),
    (
        AttendanceLabel.PROMOTING,
        re.compile(
            r"\b(?:use|with) (?:my |our |the )?(?:promo |discount )?code\b"
            r"|\bdiscount code\b|\bearly[- ]bird (?:tickets?|pricing|rates?)\b"
            r"|\b(?:get|grab|buy|secure) your (?:tickets?|pass(?:es)?|seats?)\b"
            r"|\bregistration (?:is )?(?:now )?open\b|\bregister (?:now|today|here)\b"
            r"|\b\d{1,2}% off\b",
            re.IGNORECASE,
        ),
    ),
    (
        AttendanceLabel.ORGANIZER,
        re.compile(
            r"\b(?:we'?re|we are) (?:hosting|organi[sz]ing|putting on|thrilled to host)\b"
            r"|\b(?:i'?m|i am) (?:organi[sz]ing|co-organi[sz]ing|the (?:lead )?organi[sz]er)\b"
            r"|\bproud to (?:host|organi[sz]e)\b|\bon behalf of the organi[sz]ing (?:team|committee)\b",
            re.IGNORECASE,
        ),
    ),
    (
        AttendanceLabel.SPEAKING,
        re.compile(
            rf"\b{_I} (?:speaking|presenting|moderating|giving a talk|on (?:a |the )?(?:panel|stage))\b"
            r"|\bmy (?:talk|session|keynote|workshop|fireside chat) (?:at|on)\b"
            r"|\bcatch me (?:on stage|speaking|on the panel)\b",
            re.IGNORECASE,
        ),
    ),
    (
        AttendanceLabel.EXHIBITING,
        re.compile(
            r"\b(?:visit|find|stop by|come see|swing by|meet) (?:us|me|our team) at (?:the |our )?booth\b"
            r"|\bat our booth\b|\bbooth (?:#|no\.? ?)?\d+\b",
            re.IGNORECASE,
        ),
    ),
    (
        AttendanceLabel.ATTENDED,
        re.compile(
            r"\b(?:just )?(?:back|home) from\b|\b(?:key |my )?takeaways from\b"
            r"|\bwhat an? (?:week|time|few days|event|experience) at\b|\brecap(?:ping)? (?:of|from)\b"
            r"|\b(?:great|amazing|incredible|fantastic) (?:time|week|few days) at\b"
            r"|\bthank(?:s| you) to everyone (?:i|we) met at\b",
            re.IGNORECASE,
        ),
    ),
    (
        AttendanceLabel.ATTENDING,
        re.compile(
            r"\bheading (?:to|over to|out to)\b|\bsee (?:you|y'?all|everyone) at\b"
            rf"|\b{_I} (?:at|attending|going to|in town for)\b"
            r"|\b(?:excited|can'?t wait|looking forward) to (?:attend|be at|head to|attending|being at)\b"
            r"|\bwho else is (?:going|heading) to\b|\blet'?s (?:meet|connect|grab (?:a )?coffee) at\b",
            re.IGNORECASE,
        ),
    ),
]

# When every match means the author is there, the most specific one is kept.
_MOST_SPECIFIC = [
    AttendanceLabel.SPEAKING,
    AttendanceLabel.EXHIBITING,
    AttendanceLabel.ATTENDED,
    AttendanceLabel.ATTENDING,
]


# Phones and many editors type a curly apostrophe; the rules are written with the straight one.
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


def label(text: str) -> AttendanceLabel | None:
    """The post's label when the rules are sure of it, otherwise None (Claude decides)."""
    text = text.translate(_APOSTROPHES)
    matched = {name for name, pattern in _RULES if pattern.search(text)}
    if len(matched) == 1:
        return matched.pop()
    if matched and matched <= QUALIFYING_LABELS:
        return next(name for name in _MOST_SPECIFIC if name in matched)
    return None


NOT_BUYERS = ("student", "intern", "open to work", "seeking new opportunities", "looking for new opportunities")
"""Headline words that rule anyone out of any B2B ICP, on top of the ICP's own exclusions."""


def prescreen(headline: str | None, icp: Icp, *, own_company: str | None = None, what: str = "headline") -> str | None:
    """Why this headline (or, after enrichment, `what="title"`) can't match the ICP, or None. Only clear signs count:
    Apollo's title decides the rest."""
    if not headline:
        return None
    for word in [*icp.exclude_title_keywords, *NOT_BUYERS]:
        if mentions(headline, word):
            return f"{what} says {word!r}"
    if own_company and mentions(headline, own_company):
        return "works at the requester's company"
    return None


def mentions(text: str, phrase: str) -> bool:
    """Whether `phrase` appears in `text` as whole words, ignoring case and spacing ("demand gen" in "Demand  Gen")."""
    words = phrase.casefold().split()
    if not words:
        return False
    pattern = r"\s+".join(re.escape(word) for word in words)
    return re.search(rf"(?<!\w){pattern}(?!\w)", text.casefold()) is not None
