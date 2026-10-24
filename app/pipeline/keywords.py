"""The LinkedIn searches for one event: at most four, most specific first (issue #4).

LinkedIn's post search understands quoted phrases, hashtags, AND and OR. People rarely type an event's full name, so
the searches mix the full name, the official hashtag, attendance phrases (before and during the event) or recap
phrases (during and after it), and the short name people actually use.
"""

from datetime import date

from app.schemas import EventSuggestion

MAX_QUERIES = 4
GOING = '("see you at" OR "heading to" OR attending OR speaking OR booth)'
WENT = '("back from" OR takeaways OR recap OR highlights)'


def phrase(text: str) -> str:
    """`text` as one quoted phrase. Quotes inside it would end the phrase early, so they become spaces."""
    clean = " ".join(text.replace('"', " ").split())
    return f'"{clean}"' if clean else ""


def hashtag(text: str) -> str:
    clean = "".join(ch for ch in text if ch.isalnum())
    return f"#{clean}" if clean else ""


def build_queries(event: EventSuggestion, *, today: date) -> list[str]:
    full = phrase(event.name)
    short = phrase(event.short_name) if event.short_name else ""
    if short.lower() == full.lower():
        short = ""
    spoken = short or full
    over = event.end_date is not None and event.end_date < today
    ahead = event.start_date is not None and event.start_date > today

    candidates = [full, *(hashtag(tag) for tag in event.hashtags[:1])]
    if not over:
        candidates.append(f"{spoken} AND {GOING}")
    if not ahead:
        candidates.append(f"{spoken} AND {WENT}")
    candidates.append(short)

    queries: list[str] = []
    for query in candidates:
        if query and query.lower() not in (q.lower() for q in queries):
            queries.append(query)
    return queries[:MAX_QUERIES]
