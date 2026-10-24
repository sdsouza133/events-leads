"""Which company a website or an email address belongs to."""

import pytest

from app.domains import email_domain, is_free_mail, registrable_domain, website


@pytest.mark.parametrize(
    ("host", "domain"),
    [
        ("acme.com", "acme.com"),
        ("www.acme.com", "acme.com"),
        ("app.eu.acme.com", "acme.com"),
        ("www.acme.co.uk", "acme.co.uk"),
        ("ACME.COM.", "acme.com"),
        ("127.0.0.1", None),
        ("localhost", None),
        ("acme", None),
    ],
)
def test_the_registrable_domain_is_the_company_part_of_a_host(host: str, domain: str | None) -> None:
    assert registrable_domain(host) == domain


@pytest.mark.parametrize(
    ("typed", "parsed"),
    [
        ("acme.com", ("https://acme.com/", "acme.com")),
        ("  https://www.acme.co.uk/about?ref=x#top ", ("https://www.acme.co.uk/about", "acme.co.uk")),
        ("http://Shop.Acme.com:8080", ("http://shop.acme.com:8080/", "acme.com")),
    ],
)
def test_a_website_is_read_however_it_was_typed(typed: str, parsed: tuple[str, str]) -> None:
    assert website(typed) == parsed


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "acme",
        "acme .com",
        "http://127.0.0.1/",
        "http://localhost:8000",
        "ftp://acme.com",
        "https://user:secret@acme.com",
        "https://acme.com:99999",
        "https://" + "a" * 2050 + ".com",
    ],
)
def test_anything_but_a_public_web_address_is_refused(typed: str) -> None:
    assert website(typed) is None


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("jo@acme.com", "acme.com"),
        ("jo@mail.acme.co.uk", "acme.co.uk"),
        ("jo", None),
        ("@acme.com", None),
        ("jo@", None),
        ("jo@localhost", None),
    ],
)
def test_an_address_belongs_to_its_mailbox_domain(email: str, domain: str | None) -> None:
    assert email_domain(email) == domain


def test_personal_and_throwaway_mailboxes_are_free_mail() -> None:
    assert is_free_mail("gmail.com")
    assert is_free_mail("Outlook.com")
    assert is_free_mail("mailinator.com")
    assert not is_free_mail("acme.com")
