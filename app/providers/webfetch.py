"""Fetch a requester's website without letting anyone point us at our own network (SSRF).

The rules, applied to the first request and to every redirect:

- only `http` on port 80 and `https` on port 443, and no credentials in the URL;
- the host is resolved here, and every address it resolves to must be public: no private, loopback, link-local,
  carrier-grade NAT, multicast, reserved or unspecified ranges, including IPv4 hidden inside IPv6 (mapped, 6to4,
  Teredo, NAT64);
- we connect to the address we checked and send the hostname in the Host header and TLS SNI, so the name can't
  resolve to something else between the check and the connection (DNS rebinding);
- redirects are followed by hand, at most 3;
- at most 2 MB is read, and the whole fetch (lookups, redirects and the body) has 10 seconds. The caller gets the
  page or a FetchError by then, whatever the site does: the attempt runs on its own thread, and when time is up its
  connection is shut down (httpx's timeouts apply to each read, so a site sending a byte at a time could otherwise
  hold it open for as long as it liked);
- only HTML and plain text are accepted.
"""

import contextlib
import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx2
from bs4 import BeautifulSoup, Tag

from app.providers.types import FetchedPage

Resolver = Callable[[str, int], list[str]]

DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10.0
FETCH_THREADS = 4
MAX_TEXT_CHARS = 20_000
MAX_LINKS = 200
ACCEPTED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
REDIRECT_CODES = {301, 302, 303, 307, 308}
USER_AGENT = "events-leads/0.1 (reads a company's public pages once, when its own staff ask)"

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NOISE_TAGS = ["script", "style", "noscript", "svg", "template", "iframe", "canvas"]


class FetchError(Exception):
    """The page couldn't be fetched. The message is safe to show the requester."""


class BlockedAddressError(FetchError):
    """The link points at an address we refuse to connect to."""


class _Target(NamedTuple):
    parts: SplitResult
    host: str
    port: int


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"{host} could not be found") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return is_public_address(str(embedded))
    return ip.is_global and not (
        ip.is_multicast or ip.is_reserved or ip.is_link_local or ip.is_loopback or ip.is_private or ip.is_unspecified
    )


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]  # (server, client): the client is where traffic would go
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _check_url(url: str) -> _Target:
    try:
        parts = urlsplit(url.strip())
        explicit_port = parts.port
    except ValueError as exc:
        raise FetchError("that doesn't look like a valid web address") from exc
    if parts.scheme not in DEFAULT_PORTS:
        raise FetchError("only http and https links can be read")
    if not parts.hostname:
        raise FetchError("that link has no host name")
    if parts.username or parts.password:
        raise FetchError("links with a username or password can't be read")
    port = explicit_port or DEFAULT_PORTS[parts.scheme]
    if port != DEFAULT_PORTS[parts.scheme]:
        raise FetchError("only the standard web ports (80 and 443) can be read")
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise FetchError("that host name isn't valid") from exc
    return _Target(parts, host, port)


class SafeFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx2.BaseTransport | None = None,
        max_bytes: int = MAX_BYTES,
        timeout_seconds: float = TIMEOUT_SECONDS,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self._resolver = resolver
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        # trust_env=False: proxy settings from the environment would bypass the address checks. No keep-alive: every
        # request opens its own connection, so the attempt it belongs to can shut it down (see _Cutoff).
        self._client = httpx2.Client(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            limits=httpx2.Limits(max_keepalive_connections=0),
        )
        self._attempts = ThreadPoolExecutor(max_workers=FETCH_THREADS, thread_name_prefix="webfetch")

    def close(self) -> None:
        self._client.close()
        self._attempts.shutdown(wait=False, cancel_futures=True)

    def fetch(self, url: str) -> FetchedPage:
        deadline = time.monotonic() + self._timeout  # one budget for every lookup, redirect and the body
        cutoff = _Cutoff()
        attempt = self._attempts.submit(self._attempt, url, deadline, cutoff)
        try:
            return attempt.result(timeout=self._timeout)
        except TimeoutError as exc:
            attempt.cancel()  # if it never started (every thread busy with slow sites)
            cutoff.cut()  # frees its thread: the read in progress ends
            raise FetchError("the site took too long to answer") from exc

    def _attempt(self, url: str, deadline: float, cutoff: "_Cutoff") -> FetchedPage:
        current = url
        for _ in range(self._max_redirects + 1):
            target = _check_url(current)
            address = self._public_address(target)
            extensions: dict[str, Any] = {"trace": cutoff.trace}
            if target.parts.scheme == "https":
                extensions["sni_hostname"] = target.host
            try:
                with self._client.stream(
                    "GET",
                    _url_for_address(target, address),
                    headers={"Host": target.host, "User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
                    extensions=extensions,
                    timeout=_time_left(deadline),  # connect, send and each read
                ) as response:
                    if response.status_code in REDIRECT_CODES and "location" in response.headers:
                        current = _redirect_target(current, response.headers["location"])
                        continue
                    return self._read_page(current, response, deadline)
            # Connection, TLS, protocol and decoding failures, while connecting or while reading the body.
            except httpx2.TimeoutException as exc:
                raise FetchError("the site took too long to answer") from exc
            except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
                raise FetchError(f"{target.host} could not be reached") from exc
        raise FetchError("the site redirected too many times")

    def _public_address(self, target: _Target) -> str:
        addresses = self._resolver(target.host, target.port)
        if not addresses:
            raise FetchError(f"{target.host} could not be found")
        if not all(is_public_address(a) for a in addresses):
            raise BlockedAddressError(f"{target.host} points to a private or reserved network address")
        # Prefer IPv4: it's what most hosting egress supports.
        return sorted(addresses, key=lambda a: ipaddress.ip_address(a).version)[0]

    def _read_page(self, url: str, response: httpx2.Response, deadline: float) -> FetchedPage:
        if response.status_code >= 400:  # noqa: PLR2004 (HTTP error range)
            raise FetchError(f"the site answered with an error ({response.status_code})")
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in ACCEPTED_TYPES:
            raise FetchError("that link isn't a web page")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > self._max_bytes:
                raise FetchError("the page is too large to read")
            if time.monotonic() > deadline:
                raise FetchError("the site took too long to answer")
        try:
            text = bytes(body).decode(response.charset_encoding or "utf-8", errors="replace")
        except (LookupError, ValueError):  # the site sets the charset: it may be unknown, or no text codec (idna)
            text = bytes(body).decode("utf-8", errors="replace")
        return extract_page(url, response.status_code, content_type, text)


class _Cutoff:
    """The sockets one fetch attempt opened, so the caller can shut them down when it stops waiting.

    It holds the socket objects, not their file numbers: shutting down one that was already closed fails harmlessly,
    where a reused number could belong to someone else's connection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._cut = False

    def trace(self, event: str, info: dict[str, Any]) -> None:
        """httpx's trace hook, called on the attempt's thread as it connects (and again once TLS is up)."""
        if event not in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            return
        sock = info["return_value"].get_extra_info("socket")
        with self._lock:
            self._sockets.append(sock)
            too_late = self._cut
        if too_late:
            _shut(sock)

    def cut(self) -> None:
        with self._lock:
            self._cut = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shut(sock)


def _shut(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):  # already closed, or handed over to TLS
        sock.shutdown(socket.SHUT_RDWR)


def _time_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchError("the site took too long to answer")
    return left


def _redirect_target(current: str, location: str) -> str:
    try:
        return urljoin(current, location)
    except ValueError as exc:  # e.g. an unclosed IPv6 bracket
        raise FetchError("the site redirected to an invalid address") from exc


def _url_for_address(target: _Target, address: str) -> str:
    netloc = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address  # noqa: PLR2004
    return urlunsplit((target.parts.scheme, netloc, target.parts.path or "/", target.parts.query, ""))


def extract_page(url: str, status: int, content_type: str, body: str) -> FetchedPage:
    if content_type == "text/plain":
        return FetchedPage(url=url, status=status, title=None, description=None, text=_squash(body))
    soup = BeautifulSoup(body, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    description = None
    meta = soup.find("meta", attrs={"name": "description"})
    if isinstance(meta, Tag):
        description = str(meta.get("content") or "").strip() or None
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        try:
            absolute = urljoin(url, str(anchor["href"])).split("#")[0]
        except ValueError:  # a malformed href is the site's problem, not a reason to fail the page
            continue
        if absolute.startswith(("http://", "https://")) and absolute not in links:
            links.append(absolute)
        if len(links) >= MAX_LINKS:
            break
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    return FetchedPage(
        url=url, status=status, title=title, description=description, text=_squash(soup.get_text(" ")), links=links
    )


def _squash(text: str) -> str:
    return " ".join(text.split())[:MAX_TEXT_CHARS]


# Pages that describe who a company sells to, in rough order of usefulness.
FOLLOWUP_HINTS = (
    "customers",
    "case-stud",
    "about",
    "pricing",
    "solutions",
    "industries",
    "who-we",
    "use-cases",
    "product",
    "platform",
    "company",
)


def pick_followup_links(page: FetchedPage, limit: int = 4) -> list[str]:
    """Up to `limit` same-site links worth reading after the home page (About, Customers, Pricing and the like)."""
    home = _site(page.url)
    ranked: list[tuple[int, str]] = []
    for link in page.links:
        if _site(link) != home:
            continue
        path = urlsplit(link).path.lower()
        rank = next((i for i, hint in enumerate(FOLLOWUP_HINTS) if hint in path), None)
        if rank is not None and link.rstrip("/") != page.url.rstrip("/"):
            ranked.append((rank, link))
    seen: list[str] = []
    for _, link in sorted(ranked):
        if link not in seen:
            seen.append(link)
    return seen[:limit]


def _site(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")
