"""Shared HTTP handling for the JSON APIs (Apify, Apollo, Reoon, Resend)."""

from collections.abc import Callable
from typing import Any

import httpx2

from app.providers.types import ProviderError

TIMEOUT_SECONDS = 30.0
RETRY_STATUSES = frozenset({408, 425, 429})  # request timeout, too early, rate limited; plus every 5xx

RetryIf = Callable[[httpx2.Response], bool]


def client(
    base_url: str, *, headers: dict[str, str] | None = None, transport: httpx2.BaseTransport | None = None
) -> httpx2.Client:
    return httpx2.Client(base_url=base_url, headers=headers or {}, timeout=TIMEOUT_SECONDS, transport=transport)


def json_or_raise(response: httpx2.Response, service: str, *, retry_if: RetryIf | None = None) -> Any:
    """The decoded body, or a ProviderError that says whether retrying can help. Never includes the request URL
    or headers in the message, because some services carry the API key there.

    `retry_if` marks a service's own "try again later" answers among its other 4xx responses.
    """
    status = response.status_code
    if status in RETRY_STATUSES or status >= 500 or (retry_if is not None and retry_if(response)):  # noqa: PLR2004
        raise ProviderError(f"{service} is busy or failing ({status})", retryable=True)
    if response.status_code in (401, 403):
        raise ProviderError(f"{service} rejected our credentials ({response.status_code})", retryable=False)
    if response.status_code >= 400:  # noqa: PLR2004
        raise ProviderError(f"{service} refused the request ({response.status_code})", retryable=False)
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{service} sent a response we couldn't read", retryable=True) from exc


def send(request: Any, service: str, *, retry_if: RetryIf | None = None) -> Any:
    """Run `request()` (a call on an httpx2 client) and decode it, turning network failures into retryable errors."""
    try:
        response = request()
    except httpx2.TransportError as exc:
        # The request may have reached the service before the connection failed: its outcome, and cost, are unknown.
        raise ProviderError(f"couldn't reach {service}", retryable=True, cost_usd=None) from exc
    return json_or_raise(response, service, retry_if=retry_if)
