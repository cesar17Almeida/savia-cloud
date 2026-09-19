"""HTTP link: the LoRa hop tunnelled through the phone (LINK_MODE=http).

Same wire-v2 frames as over TTN; a downlink only travels as the answer to an uplink.
"""
from __future__ import annotations

import base64
import json
import struct
import time
from dataclasses import replace

import pytest

from app.adapters.dataset.forecast import DATA_PATH, replay_row_now
from app.adapters.ttn import codec
from app.factory import create_app
from tests.conftest import ADMIN_PASSWORD

DEV = "savia-estacion-01"
HOUR = 3600
BOOT = bytes.fromhex("020600000000")
PING = bytes([codec.VERSION, codec.UP_FORECAST, 0xFF, 0xFF])     # forecast, value unknown
CFG_ACK = bytes([codec.VERSION, codec.UP_CFG_ACK, 1, 0])


@pytest.fixture
def link_settings(settings):
    return replace(settings, link_mode="http", forecast_source="dataset")


@pytest.fixture
def link_client(link_settings):
    return create_app(link_settings).test_client()


def _up(client, frame: bytes, dev=DEV, headers=None, **extra):
    body = {"device_id": dev, "f_port": 8,
            "frm_payload": base64.b64encode(frame).decode(), **extra}
    return client.post("/link/uplink", json=body, headers=headers or {})


def _downlink_bytes(resp) -> bytes:
    return base64.b64decode(resp.get_json()["downlink"]["frm_payload"])


def _login(client):
    client.post("/home/login", data={"user": "admin", "password": ADMIN_PASSWORD})


def _states(client, kind: str) -> list[str]:
    svc = client.application.config["SERVICES"]
    return [d.state for d in svc.panel.downlinks(DEV, 10) if d.kind == kind]


def test_link_is_a_404_over_ttn(client):
    assert _up(client, BOOT).status_code == 404


def test_link_token_is_checked_when_set(link_settings):
    client = create_app(replace(link_settings, link_secret="lsecret")).test_client()
    assert _up(client, BOOT).status_code == 401
    assert _up(client, BOOT, headers={"X-Link-Token": "nope"}).status_code == 401
    assert _up(client, BOOT, headers={"X-Link-Token": "lsecret"}).status_code == 200


def test_malformed_bodies_are_a_400(link_client):
    assert link_client.post("/link/uplink", json={"frm_payload": "AgY="}).status_code == 400
    assert link_client.post("/link/uplink", json={"device_id": DEV}).status_code == 400
    r = link_client.post("/link/uplink", json={"device_id": DEV, "frm_payload": "%%%"})
    assert r.status_code == 400 and r.is_json


def test_undecodable_frame_is_still_logged(link_client):
    r = _up(link_client, b"\x02\xee\x00")
    assert r.status_code == 200 and r.get_json()["type"] == "unknown"
    svc = link_client.application.config["SERVICES"]
    assert svc.panel.uplinks(DEV, 1)[0].u_type == "unknown"


def test_boot_is_answered_with_the_clock_in_the_same_response(link_client):
    r = _up(link_client, BOOT, seq=1)
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["type"] == "boot"
    assert body["downlink"]["f_port"] == 8 and body["downlink"]["kind"] == "time_ta"
    payload = _downlink_bytes(r)
    assert len(payload) == 8 and payload[:2] == bytes([codec.VERSION, codec.DN_TIME_TA])
    assert abs(int.from_bytes(payload[2:6], "big") - time.time()) < 120
    assert _states(link_client, "time_ta") == ["delivered"]

    # Nothing else queued: the next RX window stays empty, and the 6 h gap holds.
    r = _up(link_client, PING, seq=2)
    assert r.get_json() == {"ok": True, "type": "forecast", "downlink": None}
    assert _states(link_client, "time_ta") == ["delivered"]


def test_a_retried_uplink_gets_the_same_answer_once(link_client):
    """The phone repeats a POST whose answer got lost: same downlink, one log row."""
    first = _up(link_client, BOOT, seq=7)
    again = _up(link_client, BOOT, seq=7)
    assert _downlink_bytes(again) == _downlink_bytes(first)
    svc = link_client.application.config["SERVICES"]
    assert len(svc.panel.uplinks(DEV, 10)) == 1
    assert _up(link_client, BOOT, seq=8).get_json()["downlink"] is not None   # a real reboot


def test_panel_sync_queues_the_dataset_window_and_the_next_uplink_takes_it(link_client):
    _up(link_client, BOOT, utc_offset_min=-300)              # enrol + clock sync
    _login(link_client)
    assert link_client.post(f"/home/stations/{DEV}/downlink",
                            data={"tab": "resumen"}).status_code == 302
    svc = link_client.application.config["SERVICES"]
    assert svc.link_outbox.pending(DEV) == 1
    assert _states(link_client, "time_ta") == ["scheduled", "delivered"]

    r = _up(link_client, PING)
    assert r.get_json()["downlink"]["kind"] == "time_ta"
    payload = _downlink_bytes(r)
    assert len(payload) == 80 and payload[6] == 48 and payload[7] == 24

    # TA bytes = the training-set rows for the hour under the FIXED replay offset
    # (+120, as in the firmware), int8-rounded; the station's own -300 plays no part.
    ta = json.loads(DATA_PATH.read_text())["ta"]
    row = replay_row_now(int.from_bytes(payload[2:6], "big"), 120)
    expected = [codec._to_int8(t) for t in ta[row - 47: row + 25]]
    assert list(payload[8:]) == expected

    assert svc.link_outbox.pending(DEV) == 0
    assert _states(link_client, "time_ta") == ["delivered", "delivered"]
    assert _up(link_client, PING).get_json()["downlink"] is None


def test_config_goes_scheduled_delivered_applied(link_client):
    _up(link_client, BOOT)
    svc = link_client.application.config["SERVICES"]
    svc.config_downlink.run(DEV, {"lora_period_s": 600}, int(time.time()))
    assert _states(link_client, "config") == ["scheduled"]

    r = _up(link_client, PING)
    assert r.get_json()["downlink"]["kind"] == "config"
    assert _downlink_bytes(r) == bytes.fromhex("0202050400000258")
    assert _states(link_client, "config") == ["delivered"]
    assert svc.panel.config_state(DEV)["state"] == "delivered"   # still awaits the CFG_ACK

    assert _up(link_client, CFG_ACK).get_json()["type"] == "cfg_ack"
    assert _states(link_client, "config") == ["applied"]
    assert svc.panel.config_state(DEV)["state"] == "applied"


def test_downlinks_leave_oldest_first_one_per_uplink(link_client):
    _up(link_client, BOOT)
    svc = link_client.application.config["SERVICES"]
    svc.config_downlink.run(DEV, {"sleep_s": 100}, int(time.time()))
    svc.config_downlink.run(DEV, {"sleep_s": 200}, int(time.time()))
    assert _downlink_bytes(_up(link_client, PING)).hex().endswith("00000064")
    assert _downlink_bytes(_up(link_client, PING)).hex().endswith("000000c8")
    assert _up(link_client, PING).get_json()["downlink"] is None


def test_utc_offset_in_the_body_lands_on_the_station(link_client):
    _up(link_client, BOOT, utc_offset_min=120)
    svc = link_client.application.config["SERVICES"]
    assert svc.panel.station(DEV).utc_offset_min == 120
    for ignored in (5000, "120", True, None):                # out of range / not an int
        assert _up(link_client, PING, utc_offset_min=ignored).status_code == 200
        assert svc.panel.station(DEV).utc_offset_min == 120


def test_forward_flow_soil_uplinks_store_readings(link_client):
    latest = int(time.time()) // HOUR * HOUR
    recs = [(latest - i * HOUR, 0.80 - 0.001 * i, 0.78, 24.5) for i in range(8)]
    for i in range(0, len(recs), 4):
        frame = bytes([codec.VERSION, codec.UP_SOIL, 4]) + b"".join(
            struct.pack(">IHHh", ts, round(a * 1000), round(b * 1000), round(ta * 10))
            for ts, a, b, ta in recs[i:i + 4])
        assert _up(link_client, frame).get_json()["type"] == "soil"
    svc = link_client.application.config["SERVICES"]
    rows = svc.panel.readings(DEV, 100)
    assert len(rows) == 8 and rows[0].ts_hour_s == latest and rows[0].hs10 == 0.8
    assert svc.panel.station(DEV).last_rssi is None           # no gateway on this link


def test_the_station_mode_follows_what_the_node_sends(link_client):
    svc = link_client.application.config["SERVICES"]
    _up(link_client, BOOT)
    assert svc.panel.station(DEV).mode == "forward"            # auto-enrolled default
    _up(link_client, PING)                                     # a forecast with no value proves nothing
    assert svc.panel.station(DEV).mode == "forward"
    _up(link_client, bytes([codec.VERSION, codec.UP_FORECAST]) + struct.pack(">H", 742))
    assert svc.panel.station(DEV).mode == "local"              # only a LOCAL node infers
    latest = int(time.time()) // HOUR * HOUR
    soil = bytes([codec.VERSION, codec.UP_SOIL, 1]) + struct.pack(">IHHh", latest, 800, 780, 245)
    _up(link_client, soil)
    assert svc.panel.station(DEV).mode == "forward"            # only a FORWARD node uplinks soil
