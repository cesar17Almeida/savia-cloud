"""HTTP driving adapter: Flask routes that call the application services.

Routes read the wired services from app.config["SERVICES"] (set in create_app) and
the config from app.config["SETTINGS"]. Application errors map to status codes via
a single error handler.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hmac
import time

from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException

from ...adapters.ttn import codec
from ...application.errors import AppError, InvalidInput, Unauthorized
from ...application.services import Services

bp = Blueprint("api", __name__)


def _services() -> Services:
    return current_app.config["SERVICES"]


def _settings():
    return current_app.config["SETTINGS"]


@bp.errorhandler(AppError)
def _on_app_error(err: AppError):
    return jsonify(error=type(err).__name__, message=str(err)), err.status


@bp.errorhandler(Exception)
def _on_unexpected(err: Exception):
    """Last resort: the JSON API never answers with an HTML error page."""
    if isinstance(err, HTTPException):
        return jsonify(error=type(err).__name__, message=err.description), err.code
    current_app.logger.exception("api error on %s %s", request.method, request.path)
    return jsonify(error="InternalError", message="unexpected server error"), 500


def _upstream_error(err: Exception):
    """TTN or Open-Meteo refused or did not answer (the downlink is logged as failed)."""
    current_app.logger.warning("upstream failure on %s: %s", request.path, err)
    return jsonify(error="UpstreamError", message=str(err)[:240]), 502


def _json_body() -> dict:
    """The request JSON object ({} when absent); any other JSON type is a 400."""
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise InvalidInput("the request body must be a JSON object")
    return body


def _decode_frame(raw: bytes) -> dict:
    """Decode an uplink frame; an undecodable one is still logged, as type "unknown"."""
    try:
        return codec.decode_uplink(raw)
    except ValueError:
        return {"type": "unknown"}


def _token_ok(header: str, secret: str) -> bool:
    """Constant-time shared-secret check; an empty secret disables it."""
    if not secret:
        return True
    return hmac.compare_digest(request.headers.get(header, "").encode(), secret.encode())


def _limit_arg(default: int) -> int:
    """?limit= clamped to 1..500; a malformed value falls back to the default."""
    return max(1, min(request.args.get("limit", default, type=int), 500))


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
    body = _json_body()
    user = _services().auth.register(body.get("email", ""), body.get("password", ""))
    return jsonify(id=user.id, email=user.email), 201


@bp.post("/auth/login")
def auth_login():
    body = _json_body()
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
    body = _json_body()
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
    st = _services().stations.update(g.user.id, dev_eui, _json_body())
    return jsonify(_station_json(st))


@bp.get("/stations/<dev_eui>/signal")
def station_signal(dev_eui: str):
    sig = _services().signal_query.last_signal(dev_eui)
    return jsonify(signal=sig)


@bp.get("/stations/<dev_eui>/uplinks")
@require_session
def station_uplinks(dev_eui: str):
    """Latest received uplinks (raw payload + decoded type + link quality)."""
    _services().stations.get_owned(g.user.id, dev_eui)
    ups = _services().panel.uplinks(dev_eui, _limit_arg(50))
    return jsonify(uplinks=[{
        "ts_s": u.ts_s, "type": u.u_type, "payload_hex": u.payload_hex,
        "rssi": u.rssi, "snr": u.snr,
    } for u in ups])


@bp.get("/stations/<dev_eui>/readings")
@require_session
def station_readings(dev_eui: str):
    """Latest stored hourly soil readings, newest first."""
    _services().stations.get_owned(g.user.id, dev_eui)
    rows = _services().panel.readings(dev_eui, _limit_arg(48))
    return jsonify(readings=[{
        "ts_hour_s": r.ts_hour_s, "hs10": r.hs10, "hs30": r.hs30, "ta": r.ta,
    } for r in rows])


@bp.get("/stations/<dev_eui>/forecast")
@require_session
def station_forecast(dev_eui: str):
    """Latest stored inference run (24 h HS30, VWC 0..1)."""
    _services().stations.get_owned(g.user.id, dev_eui)
    run = _services().panel.latest_forecast(dev_eui)
    if run is None:
        return jsonify(forecast=None)
    return jsonify(forecast={"run_ts_s": run.run_ts_s, "hs30": run.hs30})


@bp.post("/stations/<dev_eui>/downlink")
@require_session
def station_downlink(dev_eui: str):
    """Build + schedule the clock + TA-forecast downlink for an owned station."""
    _services().stations.get_owned(g.user.id, dev_eui)
    try:
        cmd = _services().schedule_downlink.run(dev_eui, int(time.time()))
    except AppError:
        raise
    except Exception as e:
        return _upstream_error(e)
    return jsonify(ok=True, f_port=cmd.f_port, bytes=len(cmd.payload))


@bp.post("/stations/<dev_eui>/config")
@require_session
def station_config(dev_eui: str):
    """Encode + schedule a config-patch downlink for an owned station."""
    _services().stations.get_owned(g.user.id, dev_eui)
    body = _json_body()
    try:
        cmd = _services().config_downlink.run(dev_eui, body, int(time.time()))
    except ValueError as e:
        return jsonify(error="BadRequest", message=str(e)), 400
    except AppError:
        raise
    except Exception as e:
        return _upstream_error(e)
    return jsonify(ok=True, f_port=cmd.f_port, bytes=len(cmd.payload))


# --- TTN webhook -------------------------------------------------------------

@bp.post("/ttn/uplink")
def ttn_uplink():
    """TTN webhook. Verify the shared secret, decode the payload, and persist the
    reading + the gateway RSSI/SNR (the uplink signal the station cannot measure)."""
    if not _token_ok("X-Webhook-Token", _settings().webhook_secret):
        return jsonify(error="Unauthorized", message="bad webhook token"), 401

    body = _json_body()
    dev_eui = body.get("end_device_ids", {}).get("device_id", "unknown")
    um = body.get("uplink_message", {}) or {}
    raw = base64.b64decode(um["frm_payload"]) if um.get("frm_payload") else b""
    decoded = _decode_frame(raw)

    rssi = snr = None
    mds = um.get("rx_metadata") or []
    if mds:
        best = max(mds, key=lambda m: m.get("rssi", -9999))
        rssi, snr = best.get("rssi"), best.get("snr")

    # TTN flags a confirmed uplink; the station only asks for confirmation on the
    # app's coverage ping, which is what lets the log name it apart.
    _services().ingest_uplink.handle(dev_eui, decoded, rssi, snr, int(time.time()),
                                     raw_hex=raw.hex(),
                                     confirmed=bool(um.get("confirmed")))
    return jsonify(ok=True, type=decoded.get("type"))


# --- HTTP link ---------------------------------------------------------------

def _int_or_none(value) -> int | None:
    """A JSON integer, or None for anything else (bool included)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@bp.post("/link/uplink")
def link_uplink():
    """HTTP link (LINK_MODE=http): the phone tunnels one station uplink -- the same
    wire-v2 bytes LoRa would carry -- and takes back at most one queued downlink."""
    link = _services().link_uplink
    if _settings().link_mode != "http" or link is None:
        return jsonify(error="NotFound", message="the HTTP link is not enabled"), 404
    if not _token_ok("X-Link-Token", _settings().link_secret):
        return jsonify(error="Unauthorized", message="bad link token"), 401

    body = _json_body()
    dev_id = body.get("device_id")
    if not isinstance(dev_id, str) or not dev_id.strip():
        return jsonify(error="BadRequest", message="device_id required"), 400
    payload_b64 = body.get("frm_payload")
    if not isinstance(payload_b64, str):
        return jsonify(error="BadRequest", message="frm_payload (base64) required"), 400
    try:
        raw = base64.b64decode(payload_b64, validate=True)
    except (binascii.Error, ValueError):
        return jsonify(error="BadRequest", message="frm_payload is not valid base64"), 400

    decoded = _decode_frame(raw)
    cmd = link.handle(dev_id.strip(), decoded, int(time.time()), raw_hex=raw.hex(),
                      seq=_int_or_none(body.get("seq")),
                      utc_offset_min=_int_or_none(body.get("utc_offset_min")),
                      confirmed=bool(body.get("confirmed")))
    downlink = None
    if cmd is not None:
        downlink = {
            "f_port": cmd.f_port,
            "frm_payload": base64.b64encode(cmd.payload).decode(),
            "kind": codec.downlink_kind(cmd.payload),
        }
    return jsonify(ok=True, type=decoded.get("type"), downlink=downlink)


# --- cron --------------------------------------------------------------------

@bp.post("/cron/daily")
def cron_daily():
    """External scheduler entrypoint (hourly). Runs FORWARD inference for the
    stations at their local daily hour. Protected by the X-Cron-Token secret."""
    if not _token_ok("X-Cron-Token", _settings().cron_secret):
        raise Unauthorized("bad cron token")
    force = bool(_json_body().get("force"))
    report = _services().daily_cron.run(int(time.time()), force=force)
    return jsonify(ok=not report.failed, ran=report.ran,
                   skipped=report.skipped, failed=report.failed)
