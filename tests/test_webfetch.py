import socket
from collections.abc import Callable, Iterator

import httpx2
import pytest

from app.providers.types import FetchedPage
from app.providers.webfetch import (
    MAX_TEXT_CHARS,
    BlockedAddressError,
    FetchError,
    SafeFetcher,
    extract_page,
    is_public_address,
    pick_followup_links,
    system_resolver,
)

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"

HTML = """<html><head><title>Acme | Analytics</title><meta name="description" content="Pipeline analytics.">
<script>var tracking = 1;</script><style>body{}</style></head>
<body><a href="/about">About</a> <a href="https://acme.example/pricing#plans">Pricing</a>
<a href="mailto:hi@acme.example">Mail</a> <a href="https://other.example/x">Elsewhere</a>
<p>Revenue   analytics
for B2B SaaS.</p></body></html>"""


# ---------------------------------------------------------------------------------------------- address checks
@pytest.mark.parametrize("address", [PUBLIC_V4, "1.1.1.1", PUBLIC_V6])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",  # private
        "172.16.4.1",
        "192.168.1.1",
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local: cloud metadata endpoints live here
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104 (testing that it's refused)
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::1",
        "fd00::1",  # unique local (Railway's private network is in fd00::/8)
        "fe80::1",
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "2002:0a00:0001::",  # 6to4 wrapping 10.0.0.1
        "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
    ],
)
def test_internal_and_special_addresses_are_refused(address: str) -> None:
    assert not is_public_address(address)


# ------------------------------------------------------------------------------------------------- the fetcher
Handler = Callable[[httpx2.Request], httpx2.Response]


def make_fetcher(handler: Handler, dns: dict[str, list[str]], **kwargs: int) -> SafeFetcher:
    def resolve(host: str, port: int) -> list[str]:
        if host not in dns:
            raise FetchError(f"{host} could not be found")
        return dns[host]

    return SafeFetcher(resolver=resolve, transport=httpx2.MockTransport(handler), **kwargs)


def html(body: str = HTML, status: int = 200, content_type: str = "text/html; charset=utf-8") -> httpx2.Response:
    return httpx2.Response(status, headers={"content-type": content_type}, text=body)


def test_connects_to_the_checked_address_with_the_real_host_name() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return html()

    page = make_fetcher(handler, {"acme.example": [PUBLIC_V4]}).fetch("https://acme.example/")
    request = seen[0]
    assert request.url.host == PUBLIC_V4  # pinned: no second DNS lookup can change where we connect
    assert request.headers["host"] == "acme.example"
    assert request.extensions["sni_hostname"] == "acme.example"  # TLS certificate checked for the real name
    assert page.title == "Acme | Analytics"
    assert page.description == "Pipeline analytics."


def test_ipv6_addresses_are_bracketed_in_the_url() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return html()

    make_fetcher(handler, {"acme.example": [PUBLIC_V6]}).fetch("http://acme.example/")
    assert seen[0].url.host == PUBLIC_V6
    assert "sni_hostname" not in seen[0].extensions  # plain http has no TLS


def test_a_host_resolving_to_a_private_address_is_refused() -> None:
    fetcher = make_fetcher(lambda _r: html(), {"intranet.example": ["10.1.2.3"]})
    with pytest.raises(BlockedAddressError, match="private"):
        fetcher.fetch("https://intranet.example/")


def test_one_private_address_among_public_ones_is_enough_to_refuse() -> None:
    fetcher = make_fetcher(lambda _r: html(), {"mixed.example": [PUBLIC_V4, "127.0.0.1"]})
    with pytest.raises(BlockedAddressError):
        fetcher.fetch("https://mixed.example/")


def test_a_redirect_into_the_metadata_service_is_refused() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "http://metadata.internal/latest/meta-data"})

    fetcher = make_fetcher(handler, {"acme.example": [PUBLIC_V4], "metadata.internal": ["169.254.169.254"]})
    with pytest.raises(BlockedAddressError):
        fetcher.fetch("https://acme.example/")


def test_relative_redirects_are_followed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/":
            return httpx2.Response(301, headers={"location": "/home"})
        return html()

    page = make_fetcher(handler, {"acme.example": [PUBLIC_V4]}).fetch("https://acme.example/")
    assert page.url == "https://acme.example/home"


def test_too_many_redirects_stop() -> None:
    fetcher = make_fetcher(
        lambda _r: httpx2.Response(302, headers={"location": "/again"}), {"acme.example": [PUBLIC_V4]}
    )
    with pytest.raises(FetchError, match="redirected too many times"):
        fetcher.fetch("https://acme.example/")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://acme.example/", "only http and https"),
        ("https://acme.example:8443/", "standard web ports"),
        ("http://acme.example:443/", "standard web ports"),
        ("https://user:secret@acme.example/", "username or password"),
        ("https:///no-host", "no host"),
        ("https://acme.example:99999/", "valid web address"),
    ],
)
def test_unsafe_or_odd_urls_are_refused_before_any_lookup(url: str, message: str) -> None:
    fetcher = make_fetcher(lambda _r: html(), {})
    with pytest.raises(FetchError, match=message):
        fetcher.fetch(url)


def test_unknown_hosts_fail_with_a_readable_message() -> None:
    with pytest.raises(FetchError, match="could not be found"):
        make_fetcher(lambda _r: html(), {}).fetch("https://nowhere.example/")


def test_error_pages_are_reported() -> None:
    fetcher = make_fetcher(lambda _r: html(status=404), {"acme.example": [PUBLIC_V4]})
    with pytest.raises(FetchError, match="404"):
        fetcher.fetch("https://acme.example/")


def test_non_html_content_is_refused() -> None:
    fetcher = make_fetcher(
        lambda _r: httpx2.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"),
        {"acme.example": [PUBLIC_V4]},
    )
    with pytest.raises(FetchError, match="isn't a web page"):
        fetcher.fetch("https://acme.example/brochure.pdf")


def test_oversized_pages_are_cut_off() -> None:
    fetcher = make_fetcher(lambda _r: html(body="x" * 5000), {"acme.example": [PUBLIC_V4]}, max_bytes=1000)
    with pytest.raises(FetchError, match="too large"):
        fetcher.fetch("https://acme.example/")


def test_international_host_names_are_encoded() -> None:
    seen: list[str] = []

    def resolve(host: str, port: int) -> list[str]:
        seen.append(host)
        return [PUBLIC_V4]

    SafeFetcher(resolver=resolve, transport=httpx2.MockTransport(lambda _r: html())).fetch("https://bücher.example/")
    assert seen == ["xn--bcher-kva.example"]


# ------------------------------------------------------------------------------------------- reading the page
def test_text_is_cleaned_and_scripts_dropped() -> None:
    page = extract_page("https://acme.example/", 200, "text/html", HTML)
    assert "Revenue analytics for B2B SaaS." in page.text
    assert "tracking" not in page.text
    assert page.links == ["https://acme.example/about", "https://acme.example/pricing", "https://other.example/x"]


def test_plain_text_pages_are_kept_as_text() -> None:
    page = extract_page("https://acme.example/robots.txt", 200, "text/plain", "line one\n\nline   two")
    assert (page.title, page.text, page.links) == (None, "line one line two", [])


def test_long_pages_are_truncated() -> None:
    page = extract_page("https://acme.example/", 200, "text/plain", "word " * 10_000)
    assert len(page.text) == MAX_TEXT_CHARS


def test_followup_links_prefer_customer_and_about_pages_on_the_same_site() -> None:
    page = FetchedPage(
        url="https://www.acme.example/",
        status=200,
        title=None,
        description=None,
        text="",
        links=[
            "https://www.acme.example/blog/hello",
            "https://acme.example/pricing",
            "https://www.acme.example/about-us",
            "https://www.acme.example/customers",
            "https://partner.example/customers",
            "https://www.acme.example/",
        ],
    )
    assert pick_followup_links(page, limit=3) == [
        "https://www.acme.example/customers",
        "https://www.acme.example/about-us",
        "https://acme.example/pricing",
    ]


def test_teredo_addresses_are_judged_by_the_client_they_carry() -> None:
    # Teredo stores the client IPv4 XOR-ed with all ones in the last 32 bits: f5ff:fffa is 10.0.0.5.
    assert not is_public_address("2001:0000:4136:e378:8000:63bf:f5ff:fffa")


def test_an_empty_dns_answer_is_reported_as_not_found() -> None:
    fetcher = make_fetcher(lambda _r: html(), {"empty.example": []})
    with pytest.raises(FetchError, match="could not be found"):
        fetcher.fetch("https://empty.example/")


def test_the_system_resolver_reports_unknown_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_found(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", not_found)  # no real DNS: the answer must not depend on the machine
    with pytest.raises(FetchError, match="could not be found"):
        system_resolver("no-such-host.example", 443)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx2.ConnectError("connection refused"), "acme.example could not be reached"),
        (httpx2.ReadTimeout("read timed out"), "took too long to answer"),
        (httpx2.RemoteProtocolError("peer closed connection"), "acme.example could not be reached"),
    ],
)
def test_network_failures_become_fetch_errors(error: Exception, message: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error

    with pytest.raises(FetchError, match=message):
        make_fetcher(handler, {"acme.example": [PUBLIC_V4]}).fetch("https://acme.example/")


def test_a_body_that_fails_while_reading_becomes_a_fetch_error() -> None:
    class BrokenStream(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"<html><body>half a page"
            raise httpx2.ReadError("connection reset")

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/html"}, stream=BrokenStream())

    with pytest.raises(FetchError, match="could not be reached"):
        make_fetcher(handler, {"acme.example": [PUBLIC_V4]}).fetch("https://acme.example/")


def test_an_unknown_charset_is_read_as_utf8() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = "<html><body>Caf\u00e9 analytics</body></html>".encode()
        return httpx2.Response(200, headers={"content-type": "text/html; charset=no-such-charset"}, content=body)

    page = make_fetcher(handler, {"acme.example": [PUBLIC_V4]}).fetch("https://acme.example/")
    assert page.text == "Caf\u00e9 analytics"


def test_a_malformed_redirect_is_a_fetch_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "http://[broken/"})

    with pytest.raises(FetchError, match="redirected to an invalid address"):
        make_fetcher(handler, {"acme.example": [PUBLIC_V4]}).fetch("https://acme.example/")


def test_malformed_links_are_skipped() -> None:
    page = extract_page(
        "https://acme.example/",
        200,
        "text/html",
        '<a href="http://[broken/">bad</a> <a href="/about">About</a>',
    )
    assert page.links == ["https://acme.example/about"]
