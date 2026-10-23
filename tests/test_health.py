import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import SECURITY_HEADERS, create_app


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


def test_api_docs_are_hidden_outside_dev_and_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
