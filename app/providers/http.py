"""Shared HTTP handling for the JSON APIs (Apify, Apollo, Reoon, Resend)."""

from typing import Any

import httpx2

from app.providers.types import ProviderError

TIMEOUT_SECONDS = 30.0


def client(
    base_url: str, *, headers: dict[str, str] | None = None, transport: httpx2.BaseTransport | None = None
) -> httpx2.Client:
    return httpx2.Client(base_url=base_url, headers=headers or {}, timeout=TIMEOUT_SECONDS, transport=transport)


def json_or_raise(response: httpx2.Response, service: str) -> Any:
    """The decoded body, or a ProviderError that says whether retrying can help. Never includes the request URL
    or headers in the message, because some services carry the API key there."""
    if response.status_code == 429 or response.status_code >= 500:  # noqa: PLR2004
        raise ProviderError(f"{service} is busy or failing ({response.status_code})", retryable=True)
    if response.status_code in (401, 403):
        raise ProviderError(f"{service} rejected our credentials ({response.status_code})", retryable=False)
    if response.status_code >= 400:  # noqa: PLR2004
        raise ProviderError(f"{service} refused the request ({response.status_code})", retryable=False)
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{service} sent a response we couldn't read", retryable=True) from exc


def send(request: Any, service: str) -> Any:
    """Run `request()` (a call on an httpx2 client) and decode it, turning network failures into retryable errors."""
    try:
        response = request()
    except httpx2.TransportError as exc:
        raise ProviderError(f"couldn't reach {service}", retryable=True) from exc
    return json_or_raise(response, service)
