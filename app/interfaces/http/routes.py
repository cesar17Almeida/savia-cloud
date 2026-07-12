"""HTTP driving adapter: Flask routes that call the application services.

Routes read the wired services from app.config["SERVICES"] (set in create_app) and
the config from app.config["SETTINGS"]. Application errors map to status codes via
a single error handler.
"""
from __future__ import annotations

import base64
import functools
import hmac
import time

from flask import Blueprint, current_app, g, jsonify, request

from ...adapters.ttn import codec
from ...application.errors import AppError, Unauthorized
from ...application.services import Services

bp = Blueprint("api", __name__)


def _services() -> Services:
    return current_app.config["SERVICES"]


def _settings():
    return current_app.config["SETTINGS"]


@bp.errorhandler(AppError)
def _on_app_error(err: AppError):
    return jsonify(error=type(err).__name__, message=str(err)), err.status


def require_session(fn):
    """Resolve `Authorization: Bearer <token>` into g.user or reject with 401."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        g.user = _services().auth.resolve(token)
        return fn(*args, **kwargs)
    return wrapper


@bp.get("/health")
def health():
    return jsonify(status="ok")


# --- auth --------------------------------------------------------------------

@bp.post("/auth/register")
def auth_register():
    body = request.get_json(silent=True) or {}
    user = _services().auth.register(body.get("email", ""), body.get("password", ""))
    return jsonify(id=user.id, email=user.email), 201


@bp.post("/auth/login")
def auth_login():
    body = request.get_json(silent=True) or {}
    token = _services().auth.login(body.get("email", ""), body.get("password", ""))
    return jsonify(token=token)


# --- stations ----------------------------------------------------------------

def _station_json(st) -> dict:
    return {
        "dev_eui": st.dev_eui,
        "name": st.name,
        "lat": st.lat,
        "lon": st.lon,
        "utc_offset_min": st.utc_offset_min,
        "mode": st.mode,
        "last_rssi": st.last_rssi,
        "last_snr": st.last_snr,
        "last_uplink_at": st.last_uplink_at,
    }


@bp.post("/stations/claim")
@require_session
def stations_claim():
    body = request.get_json(silent=True) or {}
    dev_eui = body.get("dev_eui", "")
    if not dev_eui:
        return jsonify(error="BadRequest", message="dev_eui required"), 400
    st = _services().stations.claim(g.user.id, dev_eui, body.get("name"))
    return jsonify(_station_json(st)), 201


@bp.get("/stations/<dev_eui>")
@require_session
def stations_get(dev_eui: str):
    st = _services().stations.get_owned(g.user.id, dev_eui)
    return jsonify(_station_json(st))


@bp.put("/stations/<dev_eui>")
@require_session
def stations_update(dev_eui: str):
    body = request.get_json(silent=True) or {}
    st = _services().stations.update(g.user.id, dev_eui, body)
    return jsonify(_station_json(st))


@bp.get("/stations/<dev_eui>/signal")
def station_signal(dev_eui: str):
    sig = _services().signal_query.last_signal(dev_eui)
    return jsonify(signal=sig)


@bp.post("/stations/<dev_eui>/downlink")
@require_session
def station_downlink(dev_eui: str):
    """Build + schedule the clock + TA-forecast downlink for an owned station."""
    _services().stations.get_owned(g.user.id, dev_eui)
    cmd = _services().schedule_downlink.run(dev_eui, int(time.time()))
    return jsonify(ok=True, f_port=cmd.f_port, bytes=len(cmd.payload))


@bp.post("/stations/<dev_eui>/config")
@require_session
def station_config(dev_eui: str):
    """Encode + schedule a config-patch downlink for an owned station."""
    _services().stations.get_owned(g.user.id, dev_eui)
    body = request.get_json(silent=True) or {}
    try:
        cmd = _services().config_downlink.run(dev_eui, body, int(time.time()))
    except ValueError as e:
        return jsonify(error="BadRequest", message=str(e)), 400
    return jsonify(ok=True, f_port=cmd.f_port, bytes=len(cmd.payload))


# --- TTN webhook -------------------------------------------------------------

@bp.post("/ttn/uplink")
def ttn_uplink():
    """TTN webhook. Verify the shared secret, decode the payload, and persist the
    reading + the gateway RSSI/SNR (the uplink signal the station cannot measure)."""
    secret = _settings().webhook_secret
    if secret and not hmac.compare_digest(request.headers.get("X-Webhook-Token", ""), secret):
        return jsonify(error="Unauthorized", message="bad webhook token"), 401

    body = request.get_json(silent=True) or {}
    dev_eui = body.get("end_device_ids", {}).get("device_id", "unknown")
    um = body.get("uplink_message", {}) or {}
    raw = base64.b64decode(um["frm_payload"]) if um.get("frm_payload") else b""

    try:
        decoded = codec.decode_uplink(raw)
    except ValueError:
        decoded = {"type": "unknown"}

    rssi = snr = None
    mds = um.get("rx_metadata") or []
    if mds:
        best = max(mds, key=lambda m: m.get("rssi", -9999))
        rssi, snr = best.get("rssi"), best.get("snr")

    _services().ingest_uplink.handle(dev_eui, decoded, rssi, snr, int(time.time()))
    return jsonify(ok=True, type=decoded.get("type"))


# --- cron --------------------------------------------------------------------

@bp.post("/cron/daily")
def cron_daily():
    """External scheduler entrypoint (hourly). Runs FORWARD inference for the
    stations at their local daily hour. Protected by the X-Cron-Token secret."""
    secret = _settings().cron_secret
    if secret and not hmac.compare_digest(request.headers.get("X-Cron-Token", ""), secret):
        raise Unauthorized("bad cron token")
    force = bool((request.get_json(silent=True) or {}).get("force"))
    done = _services().daily_cron.run(int(time.time()), force=force)
    return jsonify(ok=True, ran=done)
