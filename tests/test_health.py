from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import main
from app.main import MAX_BODY_BYTES, SECURITY_HEADERS, TOO_LARGE, create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz_is_up_without_touching_the_database(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable() -> bool:
        raise AssertionError("liveness must not query the database")

    monkeypatch.setattr(main, "database_is_reachable", unreachable)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_503_when_the_database_is_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "database_is_reachable", lambda: False)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "database unavailable"}


@pytest.mark.db
def test_readyz_is_ready_with_a_real_database(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_every_response_carries_security_headers_and_a_request_id(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["X-Request-ID"] == "abc123"
    assert "Strict-Transport-Security" not in response.headers  # only over https


def test_hsts_is_sent_when_the_site_is_served_over_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://events.example.com")
    response = TestClient(create_app()).get("/healthz")
    assert response.headers["Strict-Transport-Security"].startswith("max-age=")


def test_no_interactive_api_docs_are_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    "supplied",
    ["", "x" * 65, "abc 123", "abc\r\nInjected: yes", "<script>"],
)
def test_unsafe_request_ids_are_replaced(client: TestClient, supplied: str) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": supplied})
    issued = response.headers["X-Request-ID"]
    assert issued != supplied
    assert len(issued) == 32


def test_a_crashing_route_still_gets_headers_a_request_id_and_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    with caplog.at_level("INFO", logger="app.http"):
        response = TestClient(app).get("/boom", headers={"X-Request-ID": "req-1"})
    assert response.status_code == 500
    assert "kaboom" not in response.text  # no internals in the response
    assert response.headers["X-Request-ID"] == "req-1"
    assert response.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    records = [r for r in caplog.records if getattr(r, "request_id", None) == "req-1"]
    assert [r.getMessage() for r in records] == ["unhandled error", "request"]
    assert records[0].exc_info is not None


def app_that_reads_bodies(read: list[int]) -> FastAPI:
    app = create_app()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        read.append(len(body))
        return {"bytes": len(body)}

    return app


def chunks(total: int, size: int = 64 * 1024) -> Iterator[bytes]:
    """A body sent without a Content-Length (chunked)."""
    while total > 0:
        yield b"x" * min(size, total)
        total -= size


def test_a_body_declared_too_large_is_refused_before_it_is_read() -> None:
    read: list[int] = []
    client = TestClient(app_that_reads_bodies(read))
    response = client.post("/echo", content=b"x" * (MAX_BODY_BYTES + 1), headers={"X-Request-ID": "big-1"})
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert read == []  # the handler never ran
    assert response.headers["X-Request-ID"] == "big-1"
    assert response.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    assert client.post("/echo", content=b"x" * MAX_BODY_BYTES).json() == {"bytes": MAX_BODY_BYTES}


def test_a_body_without_a_length_is_cut_off_at_the_limit() -> None:
    read: list[int] = []
    client = TestClient(app_that_reads_bodies(read))
    response = client.post("/echo", content=chunks(MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert read == []
    assert client.post("/echo", content=chunks(MAX_BODY_BYTES)).json() == {"bytes": MAX_BODY_BYTES}


def test_a_form_too_large_is_refused_rather_than_called_unreadable(client: TestClient) -> None:
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    response = client.post("/start", content=chunks(MAX_BODY_BYTES + 1), headers=form)
    assert response.status_code == 413
