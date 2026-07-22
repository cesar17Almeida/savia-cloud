"""Web panel (driving adapter): server-rendered operator console.

Authenticates against the same AuthService as the JSON API (default operator
account admin/admin, seeded in create_app); the bearer token lives in the Flask
session cookie. Fleet-wide queries go through PanelService.
"""
from __future__ import annotations

import time

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ...adapters.ttn import codec
from ...application.errors import AppError, InsufficientData, Unauthorized
from ...application.services import Services

bp = Blueprint("web", __name__, url_prefix="/ui",
               template_folder="templates", static_folder="static")


def _services() -> Services:
    return current_app.config["SERVICES"]


@bp.app_template_filter("dt")
def _fmt_dt(ts_s, offset_min: int = 0) -> str:
    """Epoch seconds -> 'YYYY-MM-DD HH:MM' shifted by the given UTC offset."""
    if not ts_s:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts_s) + offset_min * 60))


def _current_user():
    """Resolve the session-cookie token, or None when not logged in."""
    try:
        return _services().auth.resolve(session.get("token", ""))
    except Unauthorized:
        return None


@bp.before_request
def _gate():
    """Every /ui page except login requires the operator session."""
    if request.endpoint in ("web.login", "web.static"):
        return None
    if _current_user() is None:
        return redirect(url_for("web.login"))
    return None


def _summary(u_type: str, payload_hex: str) -> str:
    """Human one-liner for a logged uplink payload."""
    try:
        d = codec.decode_uplink(bytes.fromhex(payload_hex))
    except (ValueError, TypeError):
        return "no decodificable"
    if u_type == "forecast":
        v = d.get("hs30_min")
        return "petición de ventana RX" if v is None else f"HS30 mín {v:.3f}"
    if u_type == "soil":
        recs = d.get("records", [])
        hours = ", ".join(time.strftime("%H:%M", time.gmtime(r["ts_hour_s"])) for r in recs)
        return f"{len(recs)} registro(s): {hours} UTC"
    if u_type == "coords":
        return f"lat {d['lat']:.5f}, lon {d['lon']:.5f}, offset {d['utc_offset_min']} min"
    if u_type == "cfg_ack":
        return f"{d['applied']} aplicados, {d['rejected']} rechazados"
    return "—"


# --- auth --------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        try:
            token = _services().auth.login(request.form.get("user", ""),
                                           request.form.get("password", ""))
            session["token"] = token
            return redirect(url_for("web.dashboard"))
        except AppError:
            flash("Usuario o contraseña incorrectos", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.pop("token", None)
    return redirect(url_for("web.login"))


@bp.route("/password", methods=["GET", "POST"])
def password():
    if request.method == "POST":
        try:
            _services().auth.change_password(
                _current_user(),
                request.form.get("current", ""),
                request.form.get("new", ""),
            )
            flash("Contraseña actualizada", "ok")
            return redirect(url_for("web.dashboard"))
        except AppError as e:
            flash(f"No se pudo cambiar: {e}", "error")
    return render_template("password.html")


# --- pages -------------------------------------------------------------------

@bp.get("/")
def dashboard():
    return render_template("dashboard.html", stations=_services().panel.stations())


@bp.get("/stations/<dev_eui>")
def station(dev_eui: str):
    svc = _services()
    st = svc.panel.station(dev_eui)
    ups = svc.panel.uplinks(dev_eui)
    return render_template(
        "station.html",
        st=st,
        uplinks=[(u, _summary(u.u_type, u.payload_hex)) for u in ups],
        downlinks=svc.panel.downlinks(dev_eui),
        readings=svc.panel.readings(dev_eui),
        forecast=svc.panel.latest_forecast(dev_eui),
    )


# --- actions -----------------------------------------------------------------

# Config fields sent as LoRa TLVs; (form name, cast). Empty inputs are skipped.
_CFG_FIELDS = [
    ("sleep_s", int), ("deep_sleep", int), ("capture_s", int), ("daily_hour", int),
    ("lora_period_s", int), ("inference_mode", int), ("utc_offset_min", int),
    ("irrigation_hour", int), ("lat", float), ("lon", float), ("log_level", int),
]
# TLV fields mirrored into the station row so cron gating/UI stay in sync.
_DB_MIRROR = {"utc_offset_min", "lat", "lon"}


@bp.post("/stations/<dev_eui>/config")
def station_config(dev_eui: str):
    svc = _services()
    fields: dict = {}
    try:
        for name, cast in _CFG_FIELDS:
            value = request.form.get(name, "").strip()
            if value != "":
                fields[name] = cast(value)
    except ValueError:
        flash("Valor no numérico en el formulario", "error")
        return redirect(url_for("web.station", dev_eui=dev_eui))
    if not fields:
        flash("Ningún campo a enviar", "error")
        return redirect(url_for("web.station", dev_eui=dev_eui))
    try:
        cmd = svc.config_downlink.run(dev_eui, fields, int(time.time()))
    except ValueError as e:
        flash(f"Config rechazada: {e}", "error")
        return redirect(url_for("web.station", dev_eui=dev_eui))
    mirror = {k: v for k, v in fields.items() if k in _DB_MIRROR}
    if "inference_mode" in fields:
        mirror["mode"] = "local" if fields["inference_mode"] == 1 else "forward"
    if mirror:
        svc.panel.update_station(dev_eui, mirror)
    flash(f"Config encolada por LoRa ({len(cmd.payload)} B, FPort {cmd.f_port}); "
          "se aplicará en la próxima ventana RX", "ok")
    return redirect(url_for("web.station", dev_eui=dev_eui))


@bp.post("/stations/<dev_eui>/meta")
def station_meta(dev_eui: str):
    patch = {k: request.form[k] for k in ("name", "mode") if request.form.get(k)}
    if patch:
        _services().panel.update_station(dev_eui, patch)
        flash("Estación actualizada", "ok")
    return redirect(url_for("web.station", dev_eui=dev_eui))


@bp.post("/stations/<dev_eui>/downlink")
def station_downlink(dev_eui: str):
    try:
        cmd = _services().schedule_downlink.run(dev_eui, int(time.time()))
        flash(f"Sincronización hora+TA encolada ({len(cmd.payload)} B)", "ok")
    except Exception as e:   # Open-Meteo/TTN failures surface as flash, not 500
        flash(f"No se pudo encolar: {e}", "error")
    return redirect(url_for("web.station", dev_eui=dev_eui))


@bp.post("/stations/<dev_eui>/infer")
def station_infer(dev_eui: str):
    svc = _services()
    try:
        svc.run_inference.run(svc.panel.station(dev_eui), int(time.time()))
        flash("Inferencia ejecutada; resultado almacenado y downlink encolado", "ok")
    except InsufficientData as e:
        flash(f"Datos insuficientes para inferir: {e}", "error")
    except Exception as e:
        flash(f"Inferencia fallida: {e}", "error")
    return redirect(url_for("web.station", dev_eui=dev_eui))
