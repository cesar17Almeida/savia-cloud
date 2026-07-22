"""End-to-end mock validation: a simulated station drives the full cloud pipeline.

Each test simulates the station's LoRa traffic byte-for-byte (wire v2) through the
PRODUCTION webhook endpoint and asserts what the cloud does with it. Together they
are the reproducible evidence for the thesis validation matrix (memoria cap. 10):

  1. zero-config enrollment      -- a station exists after its first uplink
  2. data accumulation           -- 48 h of SOIL uplinks build the LSTM window
  3. full FORWARD cycle          -- window -> float LSTM -> stored forecast ->
                                    TIME_TA downlink (clock + TA) back to the node
  4. gap guard                   -- <42 h of data or a >6 h hole refuses inference
  5. LOCF tolerance              -- holes <=6 h are interpolated, cycle still runs
  6. remote config w/ ack        -- config TLV downlink + CFG_ACK uplink roundtrip
  7. TTN outage resilience       -- inference survives a failed downlink push
  8. traffic budget              -- summarized wire stays ~312 B/day (TTN fair-use)

The LSTM used is the real .tflite (float); inference tests skip when no interpreter
is installed. Mock soil data follows a drying curve + daily TA sinusoid.
"""
from __future__ import annotations

import base64
import math
import struct
import time

import pytest

from app.adapters.inference.lstm import LstmInference
from app.adapters.ttn import codec
from config import Settings

_infer = LstmInference(Settings().model_path)
needs_model = pytest.mark.skipif(
    not _infer.is_available(), reason="no tflite interpreter / model available"
)

HOUR = 3600
DEV = "savia-mock"


# --- mock station: wire-level helpers ----------------------------------------

def _mock_soil(h: int) -> tuple[float, float, float]:
    """Drying soil + daily air-temperature cycle, h hours before 'now'."""
    hs10 = 0.80 - 0.0008 * (48 - h)
    hs30 = 0.78 - 0.0005 * (48 - h)
    ta = 24 + 5 * math.sin(2 * math.pi * (h % 24) / 24)
    return hs10, hs30, ta


def _soil_frame(records: list[tuple[int, float, float, float]]) -> bytes:
    """Encode a wire-v2 SOIL uplink exactly as the firmware does."""
    out = bytearray([codec.VERSION, codec.UP_SOIL, len(records)])
    for ts, hs10, hs30, ta in records:
        out += struct.pack(">IHHh", ts, round(hs10 * 1000), round(hs30 * 1000),
                           round(ta * 10))
    return bytes(out)


def _post_uplink(client, frame: bytes, dev=DEV):
    """Deliver a frame through the real TTN webhook path (secret + rx_metadata)."""
    return client.post("/ttn/uplink", json={
        "end_device_ids": {"device_id": dev},
        "uplink_message": {
            "frm_payload": base64.b64encode(frame).decode(),
            "rx_metadata": [{"rssi": -117, "snr": -8.5}],
        },
    }, headers={"X-Webhook-Token": "wsecret"})


def _feed_history(client, hours: int, hole: range = range(0), dev=DEV) -> int:
    """Simulate a station that has been running: backdated SOIL uplinks covering
    the last `hours` hours (4 records per frame, like the firmware backlog),
    optionally skipping the hours in `hole`. Returns the newest ts_hour sent."""
    latest = int(time.time()) // HOUR * HOUR
    recs = []
    for h in range(hours, 0, -1):
        if h in hole:
            continue
        hs10, hs30, ta = _mock_soil(h)
        recs.append((latest - (h - 1) * HOUR, hs10, hs30, ta))
    for i in range(0, len(recs), 4):
        assert _post_uplink(client, _soil_frame(recs[i:i + 4]), dev).status_code == 200
    return latest


def _cron_force(client):
    return client.post("/cron/daily", json={"force": True},
                       headers={"X-Cron-Token": "csecret"})


def _payloads(ttn_capture) -> list[bytes]:
    return [base64.b64decode(c["json"]["downlinks"][0]["frm_payload"])
            for c in ttn_capture if "down/push" in c["url"]]


def _inference_downlinks(ttn_capture) -> list[bytes]:
    """TIME_TA frames carrying the full TA window (vs the 8-B auto clock sync)."""
    return [p for p in _payloads(ttn_capture)
            if p[1] == codec.DN_TIME_TA and len(p) > 8]


# --- 1. zero-config enrollment ----------------------------------------------

def test_station_enrolls_on_first_uplink(client):
    """A never-seen device_id appears as a station after one uplink, with the
    gateway link quality recorded (memoria: alta sin aprovisionamiento)."""
    frame = bytes([codec.VERSION, codec.UP_FORECAST, 0xFF, 0xFF])  # RX-window ping
    assert _post_uplink(client, frame).status_code == 200
    sig = client.get(f"/stations/{DEV}/signal").get_json()["signal"]
    assert sig["rssi_dbm"] == -117 and sig["snr_db"] == -8.5


# --- 2. data accumulation ----------------------------------------------------

def test_48h_of_soil_uplinks_accumulate(client):
    """48 h of backdated SOIL frames (firmware backlog format, 4 recs/frame)
    land as 48 hourly rows keyed by ts_hour (memoria: agregacion horaria)."""
    _feed_history(client, 48)
    from app.application.services import Services  # typed access to the container
    svc: Services = client.application.config["SERVICES"]
    rows = svc.panel.readings(DEV, limit=100)
    assert len(rows) == 48
    assert all(r.hs10 is not None and r.hs30 is not None for r in rows)


# --- 3. full FORWARD cycle ---------------------------------------------------

@needs_model
def test_full_forward_cycle(client, ttn_capture):
    """The flagship E2E: 48 h of mock data -> forced cron -> float LSTM runs ->
    24 h HS30 forecast stored AND a TIME_TA downlink (clock + 48+24 TA) is pushed
    to TTN for the node (memoria: ciclo FORWARD completo)."""
    _feed_history(client, 48)
    r = _cron_force(client)
    assert r.status_code == 200 and DEV in r.get_json()["ran"]

    # Stored forecast: 24 plausible VWC values.
    svc = client.application.config["SERVICES"]
    run = svc.panel.latest_forecast(DEV)
    assert run is not None and len(run.hs30) == 24
    assert all(0.0 < v < 1.0 for v in run.hs30)

    # Downlink back to the node: decode the pushed frame and check the window.
    infs = _inference_downlinks(ttn_capture)
    assert len(infs) == 1
    payload = infs[0]
    assert payload[0] == codec.VERSION and payload[1] == codec.DN_TIME_TA
    clock = int.from_bytes(payload[2:6], "big")
    assert abs(clock - time.time()) < 120          # fresh clock sync
    assert payload[6] == 48 and payload[7] == 24   # full TA window travels


# --- 4/5. gap guard + LOCF tolerance -----------------------------------------

def test_incomplete_window_refuses_inference(client, ttn_capture):
    """A station with only 3 h of history must NOT infer: the leading 45 h gap
    exceeds MAX_SOIL_GAP_H and the cron skips it (memoria: guardas de ventana)."""
    _feed_history(client, 3)
    r = _cron_force(client)
    assert r.get_json()["ran"] == []
    assert not _inference_downlinks(ttn_capture)
    assert client.application.config["SERVICES"].panel.latest_forecast(DEV) is None


def test_gap_over_6h_refuses_inference(client, ttn_capture):
    """48 h of data with a 7 h hole in the middle is refused."""
    _feed_history(client, 48, hole=range(20, 27))
    assert _cron_force(client).get_json()["ran"] == []
    assert not _inference_downlinks(ttn_capture)


@needs_model
def test_gap_up_to_6h_is_interpolated(client, ttn_capture):
    """A 5 h hole is LOCF-filled and the cycle still completes."""
    _feed_history(client, 48, hole=range(20, 25))
    assert DEV in _cron_force(client).get_json()["ran"]
    assert len(_inference_downlinks(ttn_capture)) == 1


# --- 6. remote config with acknowledgement -----------------------------------

def test_config_downlink_and_ack_roundtrip(client, ttn_capture):
    """Backend schedules a config TLV; the node's CFG_ACK uplink closes the loop
    and both directions are visible in the logs (memoria: config remota E2E)."""
    _feed_history(client, 1)
    svc = client.application.config["SERVICES"]
    cmd = svc.config_downlink.run(DEV, {"lora_period_s": 600, "utc_offset_min": 120},
                                  int(time.time()))
    assert cmd.payload.hex() == "0202050400000258070200 78".replace(" ", "")

    # Node applies both TLVs and acks on its next uplink.
    ack = bytes([codec.VERSION, codec.UP_CFG_ACK, 2, 0])
    assert _post_uplink(client, ack).status_code == 200
    ups = svc.panel.uplinks(DEV, limit=1)
    assert ups[0].u_type == "cfg_ack"
    dls = svc.panel.downlinks(DEV, limit=1)
    assert dls[0].kind == "config" and dls[0].status == "scheduled"


# --- 7. TTN outage resilience ------------------------------------------------

@needs_model
def test_inference_survives_ttn_outage(client, monkeypatch, ttn_capture):
    """If the downlink push fails (TTN down / unknown device) the inference run
    is still stored; the downlink is logged as failed (memoria: robustez)."""
    _feed_history(client, 48)
    import app.adapters.ttn.client as ttn_client

    def _boom(*a, **k):
        raise RuntimeError("TTN unreachable")
    monkeypatch.setattr(ttn_client.TtnHttpClient, "schedule_downlink", _boom)

    assert DEV in _cron_force(client).get_json()["ran"]
    svc = client.application.config["SERVICES"]
    assert svc.panel.latest_forecast(DEV) is not None          # run persisted
    dl = svc.panel.downlinks(DEV, limit=1)[0]
    assert dl.kind == "time_ta" and dl.status.startswith("failed")


# --- 8. traffic budget --------------------------------------------------------

def test_soil_wire_stays_within_fair_use():
    """Hourly summarized wire: 13 B per single-record uplink, 43 B worst case
    (<=51 B SF12 limit), ~312 B/day at 1 uplink/h (memoria: fair-use TTN)."""
    one = _soil_frame([(1_782_000_000, 0.80, 0.78, 21.5)])
    assert len(one) == 13
    full = _soil_frame([(1_782_000_000 + i * HOUR, 0.8, 0.78, 21.5) for i in range(4)])
    assert len(full) == 43 <= 51
    daily = 24 * len(one)
    assert daily == 312
