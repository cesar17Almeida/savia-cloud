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
