"""HTTP driving adapter: Flask routes that call the application services.

Routes read the wired services from app.config["SERVICES"] (set in create_app).
"""
from __future__ import annotations

import base64
import time

from flask import Blueprint, current_app, jsonify, request

from ...adapters.ttn import codec
from ...application.services import Services
from ...domain.models import SignalReading, Uplink

bp = Blueprint("api", __name__)


def _services() -> Services:
    return current_app.config["SERVICES"]


@bp.get("/health")
def health():
    return jsonify(status="ok")


@bp.post("/ttn/uplink")
def ttn_uplink():
    """TTN webhook. Decode the payload and capture the gateway RSSI/SNR -- the
    UPLINK signal the station cannot self-measure -- then store it."""
    body = request.get_json(silent=True) or {}
    dev_id = body.get("end_device_ids", {}).get("device_id", "unknown")
    um = body.get("uplink_message", {}) or {}
    f_port = int(um.get("f_port", 0) or 0)

    raw = base64.b64decode(um["frm_payload"]) if um.get("frm_payload") else b""
    try:
        hs30 = codec.decode_uplink(raw)
    except ValueError:
        hs30 = None

    rssi = snr = None
    mds = um.get("rx_metadata") or []
    if mds:
        best = max(mds, key=lambda m: m.get("rssi", -9999))
        rssi = best.get("rssi")
        snr = best.get("snr")

    now = int(time.time() * 1000)
    uplink = Uplink(
        dev_id=dev_id,
        f_port=f_port,
        hs30_min=hs30,
        signal=SignalReading(rssi_dbm=rssi, snr_db=snr, at_ms=now),
        received_at_ms=now,
        raw=raw,
    )
    _services().ingest_uplink.handle(uplink)
    return jsonify(ok=True, hs30_min=hs30)


@bp.get("/stations/<dev_id>/signal")
def station_signal(dev_id: str):
    s = _services().signal_query.last_signal(dev_id)
    if s is None:
        return jsonify(signal=None)
    return jsonify(signal={"rssi_dbm": s.rssi_dbm, "snr_db": s.snr_db, "at_ms": s.at_ms})


@bp.post("/stations/<dev_id>/downlink")
def station_downlink(dev_id: str):
    """Build + schedule the clock + TA-forecast downlink for a station."""
    cmd = _services().schedule_downlink.run(dev_id, int(time.time()))
    return jsonify(ok=True, f_port=cmd.f_port, bytes=len(cmd.payload))
