"""Web panel: operator login, password change, fleet pages, config downlink."""
from __future__ import annotations

import base64
import struct


def _login(client, user="admin", password="admin"):
    return client.post("/ui/login", data={"user": user, "password": password},
                       follow_redirects=False)


def _soil_frame(ts_hour_s: int, hs10=0.79, hs30=0.77, ta=24.0) -> bytes:
    rec = struct.pack(">IHHh", ts_hour_s, int(hs10 * 1000), int(hs30 * 1000), int(ta * 10))
    return bytes([0x02, 0x02, 1]) + rec


def _post_uplink(client, frame: bytes, dev_id="savia"):
    return client.post("/ttn/uplink", json={
        "end_device_ids": {"device_id": dev_id},
        "uplink_message": {
            "frm_payload": base64.b64encode(frame).decode(),
            "rx_metadata": [{"rssi": -121, "snr": -11.5}],
        },
    }, headers={"X-Webhook-Token": "wsecret"})


def test_ui_requires_login(client):
    resp = client.get("/ui/")
    assert resp.status_code == 302
    assert "/ui/login" in resp.headers["Location"]


def test_default_admin_login_and_dashboard(client):
    assert _login(client).status_code == 302
    page = client.get("/ui/")
    assert page.status_code == 200
    assert b"Estaciones" in page.data


def test_bad_login_rejected(client):
    resp = client.post("/ui/login", data={"user": "admin", "password": "nope"})
    assert resp.status_code == 200
    assert "incorrectos" in resp.get_data(as_text=True)


def test_change_password_roundtrip(client):
    _login(client)
    resp = client.post("/ui/password", data={"current": "admin", "new": "s3cure"},
                       follow_redirects=False)
    assert resp.status_code == 302
    client.post("/ui/logout")
    assert _login(client, password="admin").status_code == 200   # old rejected
    assert _login(client, password="s3cure").status_code == 302  # new accepted


def test_uplink_logged_and_visible(client):
    assert _post_uplink(client, _soil_frame(1_782_000_000)).status_code == 200
    _login(client)
    page = client.get("/ui/stations/savia")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "soil" in html            # uplink type chip
    assert "0.790" in html           # stored reading rendered
    assert "-121" in html            # logged RSSI


def test_unknown_uplink_still_logged(client):
    assert _post_uplink(client, b"\x02\xee\x00").status_code == 200
    _login(client)
    html = client.get("/ui/stations/savia").get_data(as_text=True)
    assert "unknown" in html


def test_config_form_schedules_tlv_downlink(client, ttn_capture):
    _post_uplink(client, _soil_frame(1_782_000_000))
    _login(client)
    resp = client.post("/ui/stations/savia/config",
                       data={"lora_period_s": "600", "utc_offset_min": "120"},
                       follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Config encolada" in html
    assert len(ttn_capture) == 1
    # DB mirror: utc_offset_min applied to the station row.
    assert "offset 120 min" in client.get("/ui/stations/savia").get_data(as_text=True)


def test_config_form_rejects_out_of_range(client, ttn_capture):
    _post_uplink(client, _soil_frame(1_782_000_000))
    _login(client)
    resp = client.post("/ui/stations/savia/config", data={"lora_period_s": "10"},
                       follow_redirects=True)
    assert "rechazada" in resp.get_data(as_text=True)
    assert not ttn_capture


def test_api_uplinks_endpoint(client):
    from .conftest import auth_headers

    _post_uplink(client, _soil_frame(1_782_000_000))
    headers = auth_headers(client, "owner@x.y", "pw")
    client.post("/stations/claim", json={"dev_eui": "savia"}, headers=headers)
    resp = client.get("/stations/savia/uplinks", headers=headers)
    assert resp.status_code == 200
    ups = resp.get_json()["uplinks"]
    assert len(ups) == 1 and ups[0]["type"] == "soil"
    assert ups[0]["payload_hex"].startswith("0202")
