"""What every page is built from: the stylesheet, fonts and icons, all served from the site itself, because the
Content-Security-Policy allows nothing else (`app/main.py`)."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import STATIC, create_app
from app.web import icons

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize("template", sorted(TEMPLATES.glob("*.html")), ids=lambda path: path.name)
def test_templates_use_no_inline_styles_or_scripts(template: Path) -> None:
    """The CSP blocks inline styles and scripts (style-src and script-src 'self'): a page using one would break."""
    html = template.read_text()
    assert not re.search(r"\sstyle=", html)
    assert "<style" not in html
    assert not re.search(r"<script(?![^>]*\ssrc=)", html)
    assert not re.search(r"\son[a-z]+=", html)  # onclick= and friends are inline scripts too


def test_the_stylesheet_loads_nothing_from_other_sites() -> None:
    css = (STATIC / "app.css").read_text()
    assert not re.search(r"url\(\s*['\"]?(https?:)?//", css)
    assert "@import" not in css


def test_every_font_the_stylesheet_names_is_served(client: TestClient) -> None:
    fonts = re.findall(r'url\("(fonts/[^"]+)"\)', (STATIC / "app.css").read_text())
    assert len(fonts) == 4
    for font in fonts:
        response = client.get(f"/static/{font}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "font/woff2"


def test_the_pages_preload_fonts_the_stylesheet_uses() -> None:
    preloaded = re.findall(r'href="/static/(fonts/[^"]+)" as="font"', (TEMPLATES / "base.html").read_text())
    assert preloaded
    assert set(preloaded) <= set(re.findall(r'url\("(fonts/[^"]+)"\)', (STATIC / "app.css").read_text()))


def test_the_fonts_ship_with_their_licenses() -> None:
    for license_file in ("OFL-Inter.txt", "OFL-BricolageGrotesque.txt"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (STATIC / "fonts" / license_file).read_text()


def test_an_icon_is_hidden_from_screen_readers_and_takes_the_text_color() -> None:
    svg = str(icons.icon("check", 16))
    assert 'aria-hidden="true"' in svg
    assert 'focusable="false"' in svg
    assert 'width="16" height="16"' in svg
    assert 'stroke="currentColor"' in svg
    assert icons.PATHS["check"] in svg


def test_an_unknown_icon_fails_loudly() -> None:
    with pytest.raises(KeyError):
        icons.icon("no-such-icon")


@pytest.mark.parametrize("name", sorted(icons.PATHS))
def test_every_icon_is_drawable(name: str) -> None:
    assert str(icons.icon(name)).count("<svg") == 1
