"""Web panel: operator login, password change, fleet pages, config downlink."""
from __future__ import annotations

import base64
import struct


def _login(client, user="admin", password="admin"):
    return client.post("/home/login", data={"user": user, "password": password},
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
    resp = client.get("/home/")
    assert resp.status_code == 302
    assert "/home/login" in resp.headers["Location"]


def test_default_admin_login_and_dashboard(client):
    assert _login(client).status_code == 302
    page = client.get("/home/")
    assert page.status_code == 200
    assert b"Estaciones" in page.data


def test_bad_login_rejected(client):
    resp = client.post("/home/login", data={"user": "admin", "password": "nope"})
    assert resp.status_code == 200
    assert "incorrectos" in resp.get_data(as_text=True)


def test_change_password_roundtrip(client):
    _login(client)
    resp = client.post("/home/password", data={"current": "admin", "new": "s3cure"},
                       follow_redirects=False)
    assert resp.status_code == 302
    client.post("/home/logout")
    assert _login(client, password="admin").status_code == 200   # old rejected
    assert _login(client, password="s3cure").status_code == 302  # new accepted


def test_uplink_logged_and_visible(client):
    assert _post_uplink(client, _soil_frame(1_782_000_000)).status_code == 200
    _login(client)
    page = client.get("/home/stations/savia")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "soil" in html            # uplink type chip
    assert "0.790" in html           # stored reading rendered
    assert "-121" in html            # logged RSSI


def test_unknown_uplink_still_logged(client):
    assert _post_uplink(client, b"\x02\xee\x00").status_code == 200
    _login(client)
    html = client.get("/home/stations/savia").get_data(as_text=True)
    assert "unknown" in html


def test_config_form_schedules_tlv_downlink(client, ttn_capture):
    _post_uplink(client, _soil_frame(1_782_000_000))
    _login(client)
    resp = client.post("/home/stations/savia/config",
                       data={"lora_period_s": "600", "utc_offset_min": "120"},
                       follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Config encolada" in html
    assert len(ttn_capture) == 1
    # DB mirror: utc_offset_min applied to the station row.
    assert "offset 120 min" in client.get("/home/stations/savia").get_data(as_text=True)


def test_config_form_rejects_out_of_range(client, ttn_capture):
    _post_uplink(client, _soil_frame(1_782_000_000))
    _login(client)
    resp = client.post("/home/stations/savia/config", data={"lora_period_s": "10"},
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


def test_add_device_panel_only(client):
    """The add-device form creates a station without touching TTN when no keys."""
    _login(client)
    resp = client.post("/home/stations/new", data={
        "dev_id": "savia-2", "name": "Invernadero", "tz": "Europe/Madrid",
        "mode": "forward",
    }, follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Dispositivo dado de alta" in html
    assert "Invernadero" in html
    # Madrid offset lands in the station row (60 or 120 depending on DST).
    svc = client.application.config["SERVICES"]
    assert svc.panel.station("savia-2").utc_offset_min in (60, 120)


def test_add_device_with_ttn_provisioning(client, ttn_capture):
    """With OTAA keys the device is registered across the four TTN registries."""
    _login(client)
    resp = client.post("/home/stations/new", data={
        "dev_id": "savia-3", "tz": "UTC",
        "dev_eui": "70B3D57ED0000001", "join_eui": "0000000000000000",
        "app_key": "0" * 32,
    }, follow_redirects=True)
    assert "aprovisionado en TTN" in resp.get_data(as_text=True)
    urls = [c["url"] for c in ttn_capture]
    assert len(urls) == 4
    assert urls[0].endswith("/applications/savia/devices")            # identity
    assert "/js/" in urls[1] and "/ns/" in urls[2] and "/as/" in urls[3]
    assert ttn_capture[1]["json"]["end_device"]["root_keys"]["app_key"]["key"] == "0" * 32
    assert ttn_capture[2]["json"]["end_device"]["lorawan_version"] == "MAC_V1_0_2"


def test_add_device_rejects_bad_keys(client, ttn_capture):
    _login(client)
    resp = client.post("/home/stations/new", data={
        "dev_id": "savia-4", "tz": "UTC", "dev_eui": "XYZ", "join_eui": "0",
        "app_key": "1",
    })
    assert "DevEUI" in resp.get_data(as_text=True)
    assert not ttn_capture


def test_add_device_duplicate_rejected(client):
    _login(client)
    client.post("/home/stations/new", data={"dev_id": "dup-1", "tz": "UTC"})
    resp = client.post("/home/stations/new", data={"dev_id": "dup-1", "tz": "UTC"})
    assert "ya existe" in resp.get_data(as_text=True)


def test_timezone_selector_sends_tlv(client, ttn_capture):
    """The timezone form schedules a utc_offset TLV and mirrors it in the DB."""
    _post_uplink(client, _soil_frame(1_782_000_000))
    _login(client)
    resp = client.post("/home/stations/savia/timezone", data={"tz": "America/Bogota"},
                       follow_redirects=True)
    assert "America/Bogota" in resp.get_data(as_text=True)
    assert len(ttn_capture) == 1
    svc = client.application.config["SERVICES"]
    assert svc.panel.station("savia").utc_offset_min == -300  # Bogota, no DST
