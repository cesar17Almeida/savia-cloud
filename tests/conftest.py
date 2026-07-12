"""Shared fixtures: an in-memory app and stubs so no test touches the network."""
from datetime import datetime, timedelta, timezone

import pytest

from app.factory import create_app
from config import Settings

WEBHOOK_SECRET = "wsecret"
CRON_SECRET = "csecret"


@pytest.fixture
def settings():
    return Settings(
        db_url="sqlite://",             # shared in-memory engine (StaticPool)
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
    """Capture TTN downlink pushes without hitting the network."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResp(status=200)

    monkeypatch.setattr("app.adapters.ttn.client.requests.post", fake_post)
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
