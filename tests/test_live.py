"""Live communication card: live.json feeds the timeline and the in-flight notice."""
from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from app.adapters.ttn import codec
from app.factory import create_app
from app.interfaces.web.routes import _banner
from tests.conftest import ADMIN_PASSWORD

DEV = "savia-estacion-01"
BOOT = bytes.fromhex("020600000000")
PING = bytes([codec.VERSION, codec.UP_FORECAST, 0xFF, 0xFF])
FORECAST = bytes([codec.VERSION, codec.UP_FORECAST, 0x02, 0xE6])   # hs30_min 0.742


@pytest.fixture
def link_client(settings):
    app = create_app(replace(settings, link_mode="http", forecast_source="dataset"))
    return app.test_client()


def _up(client, frame: bytes):
    return client.post("/link/uplink", json={
        "device_id": DEV, "f_port": 8, "frm_payload": base64.b64encode(frame).decode()})


def _login(client):
    client.post("/home/login", data={"user": "admin", "password": ADMIN_PASSWORD})


def _live(client) -> dict:
    return client.get(f"/home/stations/{DEV}/live.json").get_json()


def test_live_json_needs_a_session(link_client):
    _up(link_client, BOOT)
    r = link_client.get(f"/home/stations/{DEV}/live.json")
    assert r.status_code == 401 and r.is_json


def test_live_json_unknown_station_is_a_404(link_client):
    _login(link_client)
    assert link_client.get("/home/stations/nope/live.json").status_code == 404


def test_live_json_shape(link_client):
    _up(link_client, BOOT)
    _login(link_client)
    link_client.post(f"/home/stations/{DEV}/downlink")
    live = _live(link_client)
    assert set(live) == {"now_s", "link_mode", "pending", "uplinks", "downlinks",
                         "forecast", "station", "banner"}
    assert live["link_mode"] == "http" and live["pending"] == 1 and live["forecast"] is None
    assert live["station"]["mode"] == "forward" and live["station"]["last_uplink_at"]

    up = live["uplinks"][0]
    assert up["type"] == "boot" and up["bytes"] == 6 and up["payload_hex"] == BOOT.hex()
    assert up["summary"] == "arranque sin hora de referencia" and up["id"]

    queued, clock = live["downlinks"]                          # newest first
    assert clock["kind"] == "time_ta" and clock["bytes"] == 8 and "ta" not in clock
    assert clock["state"] == "delivered" and clock["label"] == "entregado a la estación"
    assert clock["delivered_s"] is not None
    assert queued["state"] == "scheduled" and queued["bytes"] == 80
    assert queued["delivered_s"] is None
    assert len(queued["ta"]["past"]) == 48 and len(queued["ta"]["future"]) == 24
    assert queued["ta"]["min"] == min(queued["ta"]["past"] + queued["ta"]["future"])


def test_banner_follows_the_downlink_and_then_the_forecast(link_client):
    _up(link_client, PING)
    _login(link_client)
    link_client.post(f"/home/stations/{DEV}/downlink")
    banner = _live(link_client)["banner"]
    assert banner["state"] == "sending" and banner["meta"] == "time_ta · 80 B"
    assert "Enviando paquete por LoRa" in banner["title"]
    assert "ventana de recepción" in banner["detail"]

    _up(link_client, PING)                                     # the station takes it
    banner = _live(link_client)["banner"]
    assert banner["state"] == "delivered"
    assert banner["title"] == "Paquete entregado a la estación"


def test_redirected_page_shows_the_notice_at_once(link_client):
    """State comes from the DB, so the page after the POST already carries it."""
    _up(link_client, PING)
    _login(link_client)
    page = link_client.post(f"/home/stations/{DEV}/downlink", data={"tab": "actividad"},
                            follow_redirects=True).get_data(as_text=True)
    assert "Enviando paquete por LoRa" in page and "linkbar is-sending" in page
    assert "Actividad" in page                                 # on every tab, not just one


def test_delivered_config_still_reads_as_pending(link_client):
    """Handed to the station is not applied: the panel waits for the CFG_ACK."""
    import time

    _up(link_client, PING)
    svc = link_client.application.config["SERVICES"]
    svc.config_downlink.run(DEV, {"lora_period_s": 600}, int(time.time()))
    _up(link_client, PING)                                     # delivered
    _login(link_client)
    html = link_client.get(f"/home/stations/{DEV}").get_data(as_text=True)
    assert "pendiente de confirmación" in html and "Entregada a la estación" in html

    _up(link_client, bytes([codec.VERSION, codec.UP_CFG_ACK, 1, 0]))
    html = link_client.get(f"/home/stations/{DEV}").get_data(as_text=True)
    assert "pendiente de confirmación" not in html
    assert "confirmada por la estación" in html


def test_banner_reports_the_forecast_after_the_window():
    """Pure state machine: delivered for 6 s, then the first forecast that follows."""
    dl = {"id": 3, "state": "delivered", "kind": "time_ta", "bytes": 80,
          "delivered_s": 1000, "ta": {"past": [20], "future": [21]}}
    fetch = {"id": 8, "type": "forecast", "ts_s": 1000, "bytes": 4, "hs30_min": 0.7}
    assert _banner([fetch], [dl], 1003)["state"] == "delivered"
    assert _banner([fetch], [dl], 1010) is None                # that uplink predates it

    result = {"id": 9, "type": "forecast", "ts_s": 1015, "bytes": 4, "hs30_min": 0.742}
    banner = _banner([result, fetch], [dl], 1016)
    assert banner["state"] == "result"
    assert banner["title"].endswith("HS30 mínimo previsto 0,742")   # Spanish decimal comma
    assert _banner([result, fetch], [dl], 1015 + 600) is None

    clock_only = {**dl, "bytes": 8, "ta": None}                # a bare clock sync is not a window
    assert _banner([result, fetch], [clock_only], 1016) is None


def test_no_banner_over_ttn(client, ttn_capture, openmeteo_stub):
    """TTN never reports a delivery, so the notice exists on the HTTP link only."""
    client.post("/ttn/uplink", json={
        "end_device_ids": {"device_id": DEV},
        "uplink_message": {"frm_payload": base64.b64encode(PING).decode()},
    }, headers={"X-Webhook-Token": "wsecret"})
    _login(client)
    client.post(f"/home/stations/{DEV}/downlink")
    live = _live(client)
    assert live["link_mode"] == "ttn" and live["pending"] == 0 and live["banner"] is None
    assert [d["state"] for d in live["downlinks"]] == ["scheduled", "scheduled"]


def test_summary_tab_renders_the_live_card(link_client):
    _up(link_client, BOOT)
    _up(link_client, FORECAST)
    _login(link_client)
    html = link_client.get(f"/home/stations/{DEV}").get_data(as_text=True)
    assert "Comunicación en vivo" in html and "live.js" in html
    assert "arranque sin hora de referencia" in html           # server-rendered frames
    assert "02 06 00 00 00 00" in html                         # grouped hex payload
    assert "entregado a la estación" in html
    assert "0,742" in html                                     # on-board forecast value
    other = link_client.get(f"/home/stations/{DEV}?tab=ajustes").get_data(as_text=True)
    assert "Comunicación en vivo" not in other and "linkbar" in other
