"""Which company a website or an email address belongs to: its registrable domain ("acme.co.uk" for
"www.acme.co.uk"), read from the public suffix list that ships with tldextract, so it never goes to the network."""

from urllib.parse import urlsplit

import tldextract

_EXTRACT = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(), fallback_to_snapshot=True)
MAX_URL_LENGTH = 2048

FREE_MAIL = frozenset(
    {
        "aim.com",
        "aol.com",
        "att.net",
        "btinternet.com",
        "comcast.net",
        "duck.com",
        "fastmail.com",
        "free.fr",
        "gmail.com",
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "googlemail.com",
        "hey.com",
        "hotmail.co.uk",
        "hotmail.com",
        "hushmail.com",
        "icloud.com",
        "laposte.net",
        "libero.it",
        "live.com",
        "mac.com",
        "mail.com",
        "mail.ru",
        "me.com",
        "msn.com",
        "naver.com",
        "orange.fr",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "rediffmail.com",
        "sbcglobal.net",
        "t-online.de",
        "tutanota.com",
        "verizon.net",
        "web.de",
        "yahoo.co.uk",
        "yahoo.com",
        "yandex.com",
        "yandex.ru",
        "ymail.com",
        "zoho.com",
        # Throwaway inboxes.
        "guerrillamail.com",
        "mailinator.com",
        "sharklasers.com",
        "temp-mail.org",
        "trashmail.com",
        "yopmail.com",
    }
)
"""Personal and throwaway mailboxes: they don't show that someone works anywhere (ADR 0004)."""


def registrable_domain(host: str) -> str | None:
    """'www.acme.co.uk' -> 'acme.co.uk'. None for an IP address, 'localhost' or anything without a public suffix."""
    parts = _EXTRACT(host.strip().strip(".").lower())
    if not parts.domain or not parts.suffix:
        return None
    return f"{parts.domain}.{parts.suffix}"


def website(raw: str) -> tuple[str, str] | None:
    """A website as typed ('acme.com', 'https://www.acme.com/about') -> (its URL, its registrable domain).

    None when it isn't a public web address. The fetcher still refuses private addresses on its own (SSRF).
    """
    text = raw.strip()
    if not text or len(text) > MAX_URL_LENGTH or any(c.isspace() for c in text):
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port  # raises on a malformed port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    domain = registrable_domain(parts.hostname)
    if domain is None:
        return None
    netloc = parts.hostname.lower() + (f":{port}" if port else "")
    return f"{parts.scheme}://{netloc}{parts.path or '/'}", domain


def email_domain(email: str) -> str | None:
    """The registrable domain of an address's mailbox: 'jo@mail.acme.com' -> 'acme.com'."""
    local, at, host = email.strip().rpartition("@")
    if not at or not local or not host:
        return None
    return registrable_domain(host)


def is_free_mail(domain: str) -> bool:
    return domain.lower() in FREE_MAIL
