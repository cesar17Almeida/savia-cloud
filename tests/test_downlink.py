"""Config + time/TA downlink HTTP paths (owner-scoped), with TTN and Open-Meteo
stubbed so nothing hits the network."""
import base64

from app.adapters.ttn import codec
from tests.conftest import auth_headers


def _pushed_payload(ttn_capture):
    b64 = ttn_capture[-1]["json"]["downlinks"][0]["frm_payload"]
    return base64.b64decode(b64)


def test_config_downlink_owner_only_and_encodes_tlv(client, ttn_capture):
    h = auth_headers(client, "cfg@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-CFG"}, headers=h)

    r = client.post("/stations/EUI-CFG/config",
                    json={"sleep_s": 600, "daily_hour": 21}, headers=h)
    assert r.status_code == 200
    payload = _pushed_payload(ttn_capture)
    assert payload[:2] == bytes([codec.VERSION, codec.DN_CONFIG])
    assert payload == bytes.fromhex("0202" "0104 00000258" "0401 15".replace(" ", ""))


def test_config_downlink_rejects_out_of_range(client, ttn_capture):
    h = auth_headers(client, "cfg2@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-CFG2"}, headers=h)
    r = client.post("/stations/EUI-CFG2/config", json={"sleep_s": 5}, headers=h)
    assert r.status_code == 400


def test_config_downlink_forbidden_for_non_owner(client, ttn_capture):
    auth_headers(client, "one@x.com")  # first user (unused header)
    h_owner = auth_headers(client, "owner@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-X"}, headers=h_owner)

    h_other = auth_headers(client, "other@x.com")
    r = client.post("/stations/EUI-X/config", json={"sleep_s": 600}, headers=h_other)
    assert r.status_code == 403


def test_time_ta_downlink_builds_frame(client, ttn_capture, openmeteo_stub):
    h = auth_headers(client, "dl@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-DL"}, headers=h)

    r = client.post("/stations/EUI-DL/downlink", headers=h)
    assert r.status_code == 200 and r.get_json()["f_port"] == 8

    payload = _pushed_payload(ttn_capture)
    decoded_type = payload[1]
    assert payload[0] == codec.VERSION and decoded_type == codec.DN_TIME_TA
    # 48 past + 24 future + 8-byte header
    assert len(payload) == 8 + 48 + 24


def test_config_with_null_or_non_numeric_values_is_a_400(client, ttn_capture):
    h = auth_headers(client, "cfg3@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-CFG3"}, headers=h)
    for body in ({"sleep_s": None}, {"sleep_s": "abc"}, {"lat": "north"}, [600]):
        r = client.post("/stations/EUI-CFG3/config", json=body, headers=h)
        assert r.status_code == 400 and r.is_json, body
    r = client.post("/stations/EUI-CFG3/config", data='{"sleep_s": Infinity}',
                    content_type="application/json", headers=h)
    assert r.status_code == 400
    assert not ttn_capture


def test_time_ta_downlink_with_ttn_down_is_a_logged_502(client, monkeypatch, openmeteo_stub):
    """TTN refusing the push is an upstream failure: JSON 502 and a failed log row."""
    import app.adapters.ttn.client as ttn_client

    def _boom(*a, **k):
        raise RuntimeError("TTN downlink push failed (503): unavailable")
    monkeypatch.setattr(ttn_client.TtnHttpClient, "schedule_downlink", _boom)

    h = auth_headers(client, "dl2@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-DL2"}, headers=h)
    r = client.post("/stations/EUI-DL2/downlink", headers=h)
    assert r.status_code == 502 and r.get_json()["error"] == "UpstreamError"
    svc = client.application.config["SERVICES"]
    dl = svc.panel.downlinks("EUI-DL2", limit=1)[0]
    assert dl.kind == "time_ta" and dl.state == "failed"


def test_config_with_ttn_down_is_a_502(client, monkeypatch):
    import app.adapters.ttn.client as ttn_client

    def _boom(*a, **k):
        raise RuntimeError("TTN downlink push failed (401): unauthenticated")
    monkeypatch.setattr(ttn_client.TtnHttpClient, "schedule_downlink", _boom)

    h = auth_headers(client, "cfg4@x.com")
    client.post("/stations/claim", json={"dev_eui": "EUI-CFG4"}, headers=h)
    r = client.post("/stations/EUI-CFG4/config", json={"sleep_s": 600}, headers=h)
    assert r.status_code == 502 and r.is_json


def test_openmeteo_gap_in_the_window_is_a_clear_error(monkeypatch, settings):
    """A null temperature inside the 72 h window must not surface as a TypeError."""
    from datetime import datetime, timedelta, timezone

    import pytest

    from app.adapters.openmeteo.client import OpenMeteoForecast
    from tests.conftest import _FakeResp

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) \
        - timedelta(hours=60)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(120)]
    temps = [18.0] * 120
    temps[59] = None                                   # one hour before "now"
    monkeypatch.setattr("app.adapters.openmeteo.client.requests.get",
                        lambda url, params=None, timeout=None: _FakeResp(
                            {"hourly": {"time": times, "temperature_2m": temps}}))
    with pytest.raises(ValueError, match="missing temperatures"):
        OpenMeteoForecast(settings).fetch(39.47, -0.38)
