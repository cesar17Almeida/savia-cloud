"""Shared fixtures: an in-memory app and stubs so no test touches the network.

Set TEST_DATABASE_URL (e.g. postgresql+psycopg://postgres@127.0.0.1:55432/savia_test)
to run the whole suite against a real PostgreSQL; every test then starts from a
dropped-and-recreated schema. Without it the suite uses a private in-memory SQLite
engine per test, which needs no server.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.repository.orm import Base

from app.factory import create_app
from config import Settings

WEBHOOK_SECRET = "wsecret"
CRON_SECRET = "csecret"


TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "sqlite://")


@pytest.fixture(autouse=True)
def _fresh_schema():
    """On PostgreSQL, drop and recreate the schema before each test."""
    if TEST_DB_URL.startswith("sqlite"):
        yield
        return
    from sqlalchemy import create_engine
    engine = create_engine(TEST_DB_URL)
    Base.metadata.drop_all(engine)
    engine.dispose()
    yield


@pytest.fixture
def settings():
    return Settings(
        db_url=TEST_DB_URL,             # sqlite:// = private in-memory engine (StaticPool)
        webhook_secret=WEBHOOK_SECRET,
        cron_secret=CRON_SECRET,
        default_lat=39.47,
        default_lon=-0.38,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    return app.test_client()


class _FakeResp:
    def __init__(self, json_data=None, status=200, text=""):
        self._json = json_data or {}
        self.status_code = status
        self.text = text

    def json(self):
        return self._json


@pytest.fixture
def ttn_capture(monkeypatch):
    """Capture TTN calls (downlink pushes + device registrations) without network."""
    calls = []

    def _capture(method):
        def fake(url, json=None, headers=None, timeout=None):
            calls.append({"method": method, "url": url, "json": json})
            return _FakeResp(status=200)
        return fake

    monkeypatch.setattr("app.adapters.ttn.client.requests.post", _capture("post"))
    monkeypatch.setattr("app.adapters.ttn.client.requests.put", _capture("put"))
    return calls


@pytest.fixture
def openmeteo_stub(monkeypatch):
    """Return a flat 120 h hourly series centred on the current UTC hour."""
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=60)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(120)]
    temps = [18.0 + (i % 10) for i in range(120)]

    def fake_get(url, params=None, timeout=None):
        return _FakeResp({"hourly": {"time": times, "temperature_2m": temps}})

    monkeypatch.setattr("app.adapters.openmeteo.client.requests.get", fake_get)


def auth_headers(client, email="a@b.com", password="pw12345"):
    """Register + login, returning an Authorization header dict."""
    client.post("/auth/register", json={"email": email, "password": password})
    token = client.post("/auth/login", json={"email": email, "password": password}).get_json()["token"]
    return {"Authorization": f"Bearer {token}"}
