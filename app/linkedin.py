"""LinkedIn profile URLs, normalized so the same person is the same row whichever way the URL was written."""

import re
from urllib.parse import urlsplit

_PROFILE = re.compile(r"^/(in|pub)/([^/?#]+)", re.IGNORECASE)


def normalize_profile_url(url: str | None) -> str | None:
    """'http://linkedin.com/in/Jane-Doe-123/?trk=x' -> 'https://www.linkedin.com/in/jane-doe-123'.

    Returns None for anything that isn't a personal profile (company pages, posts, other sites).
    """
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = _PROFILE.match(parts.path)
    if not match:
        return None
    return f"https://www.linkedin.com/in/{match.group(2).lower()}"
