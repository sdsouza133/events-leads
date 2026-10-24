"""Optional second email check through Reoon (off by default). Reoon wants the API key as a query parameter, which
is why the HTTP client loggers are kept quiet (app/logging_config.py)."""

import httpx2

from app.providers import http

SERVICE = "Reoon"


class ReoonVerifier:
    def __init__(self, api_key: str, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._key = api_key
        self._http = http.client("https://emailverifier.reoon.com/api/v1", transport=transport)

    def verify(self, email: str) -> str:
        params = {"email": email, "key": self._key, "mode": "power"}
        data = http.send(lambda: self._http.get("/verify", params=params), SERVICE)
        return str(data.get("status") or "unknown").lower()
