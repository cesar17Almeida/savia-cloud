"""Register / login / claim / ownership flow over the real SQLite adapter."""
from tests.conftest import auth_headers


def test_register_login_and_duplicate(client):
    r = client.post("/auth/register", json={"email": "u@x.com", "password": "secret1"})
    assert r.status_code == 201 and r.get_json()["email"] == "u@x.com"

    # duplicate email -> 409
    assert client.post("/auth/register", json={"email": "u@x.com", "password": "z"}).status_code == 409

    ok = client.post("/auth/login", json={"email": "u@x.com", "password": "secret1"})
    assert ok.status_code == 200 and "token" in ok.get_json()

    bad = client.post("/auth/login", json={"email": "u@x.com", "password": "wrong"})
    assert bad.status_code == 401


def test_protected_route_needs_bearer(client):
    assert client.get("/stations/EUI1").status_code == 401
    assert client.get("/stations/EUI1", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_claim_and_owner_access(client):
    h = auth_headers(client, "owner@x.com")
    r = client.post("/stations/claim", json={"dev_eui": "EUI-A", "name": "Field A"}, headers=h)
    assert r.status_code == 201 and r.get_json()["name"] == "Field A"

    got = client.get("/stations/EUI-A", headers=h)
    assert got.status_code == 200 and got.get_json()["dev_eui"] == "EUI-A"

    upd = client.put("/stations/EUI-A", json={"lat": 39.5, "lon": -0.4, "mode": "local"}, headers=h)
    assert upd.status_code == 200
    body = upd.get_json()
    assert body["lat"] == 39.5 and body["mode"] == "local"


def test_claim_conflict_and_forbidden(client):
    h1 = auth_headers(client, "one@x.com")
    h2 = auth_headers(client, "two@x.com")
    assert client.post("/stations/claim", json={"dev_eui": "EUI-B"}, headers=h1).status_code == 201

    # second user cannot claim the same station
    assert client.post("/stations/claim", json={"dev_eui": "EUI-B"}, headers=h2).status_code == 409
    # ...nor read it
    assert client.get("/stations/EUI-B", headers=h2).status_code == 403


def test_claim_is_idempotent_for_owner(client):
    h = auth_headers(client, "again@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-C"}, headers=h)
    assert client.post("/stations/claim", json={"dev_eui": "EUI-C", "name": "renamed"}, headers=h).status_code == 201
    assert client.get("/stations/EUI-C", headers=h).get_json()["name"] == "renamed"
