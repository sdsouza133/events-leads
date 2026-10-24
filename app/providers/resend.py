"""Email through Resend. Each email's key is sent as Resend's Idempotency-Key, so a send repeated after a crash is
dropped by Resend instead of reaching the person twice (ADR 0002)."""

import httpx2

from app.providers import http
from app.providers.types import OutgoingEmail

SERVICE = "Resend"


class ResendMailer:
    def __init__(self, api_key: str, *, sender: str, transport: httpx2.BaseTransport | None = None) -> None:
        self._sender = sender
        self._http = http.client(
            "https://api.resend.com", headers={"Authorization": f"Bearer {api_key}"}, transport=transport
        )

    def send(self, email: OutgoingEmail) -> None:
        body: dict[str, object] = {"from": self._sender, "to": [email.to], "subject": email.subject, "text": email.text}
        if email.html:
            body["html"] = email.html
        headers = {"Idempotency-Key": email.key}
        http.send(lambda: self._http.post("/emails", json=body, headers=headers), SERVICE, retry_if=_still_sending)


def _still_sending(response: httpx2.Response) -> bool:
    """Resend's 409 `concurrent_idempotent_requests`: an earlier send with this key is still in progress (a job
    retried after a crash), so asking again later is safe. Its other 409, `invalid_idempotent_request` (the same key
    with a different email), is a bug on our side and stays final."""
    if response.status_code != 409:  # noqa: PLR2004
        return False
    try:
        error = response.json()
    except ValueError:
        return False
    return isinstance(error, dict) and error.get("name") == "concurrent_idempotent_requests"
