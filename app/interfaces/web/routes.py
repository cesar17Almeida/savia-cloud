"""Web panel (driving adapter): server-rendered operator console.

Authenticates against the same AuthService as the JSON API (default operator
account admin/admin, seeded in create_app); the bearer token lives in the Flask
session cookie. Fleet-wide queries go through PanelService.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

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

from werkzeug.exceptions import HTTPException

from ...adapters.ttn import codec
from ...application.errors import AppError, InsufficientData, Unauthorized
from ...application.services import Services
from ...domain.models import DL_APPLIED, DL_FAILED, DL_QUEUED

bp = Blueprint("web", __name__, url_prefix="/home",
               template_folder="templates", static_folder="static")

# Curated IANA zones for the station timezone selector (label, tz name).
TIMEZONES = [
    ("España (peninsular) — Europe/Madrid", "Europe/Madrid"),
    ("España (Canarias) — Atlantic/Canary", "Atlantic/Canary"),
    ("Portugal — Europe/Lisbon", "Europe/Lisbon"),
    ("Francia — Europe/Paris", "Europe/Paris"),
    ("Reino Unido — Europe/London", "Europe/London"),
    ("Colombia — America/Bogota", "America/Bogota"),
    ("México (centro) — America/Mexico_City", "America/Mexico_City"),
    ("Argentina — America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
    ("Chile — America/Santiago", "America/Santiago"),
    ("Perú — America/Lima", "America/Lima"),
    ("Ecuador — America/Guayaquil", "America/Guayaquil"),
    ("EE. UU. (este) — America/New_York", "America/New_York"),
    ("EE. UU. (oeste) — America/Los_Angeles", "America/Los_Angeles"),
    ("UTC", "UTC"),
]


def _tz_offset_min(tz_name: str) -> int:
    """Current UTC offset of an IANA zone, in minutes (DST-aware at call time)."""
    delta = datetime.now(ZoneInfo(tz_name)).utcoffset()
    return int(delta.total_seconds() // 60)


def _services() -> Services:
    return current_app.config["SERVICES"]


@bp.app_template_filter("dt")
def _fmt_dt(ts_s, offset_min: int = 0) -> str:
    """Epoch seconds -> 'YYYY-MM-DD HH:MM' shifted by the given UTC offset."""
    if not ts_s:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts_s) + offset_min * 60))


@bp.app_context_processor
def _nav():
    """Sidebar highlight: every station page lives under the 'Estaciones' entry."""
    endpoint = (request.endpoint or "").removeprefix("web.")
    return {"nav_active": "password" if endpoint == "password" else "stations"}


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


# Downlink lifecycle as shown in the panel.
_DL_LABEL = {DL_QUEUED: "en cola", DL_APPLIED: "aplicada", DL_FAILED: "no enviada"}


@bp.app_template_filter("dl_state")
def _dl_state(status: str) -> str:
    """Lifecycle token of a logged downlink status ('applied: 8 ok' -> 'applied')."""
    return status.split(":", 1)[0].strip()


@bp.app_template_filter("dl_label")
def _dl_label(status: str) -> str:
    return _DL_LABEL.get(_dl_state(status), status)


@bp.app_template_filter("dl_detail")
def _dl_detail(status: str) -> str:
    """The stored status, minus its lifecycle token, in operator language."""
    _, _, rest = status.partition(":")
    return _humanize(rest.strip() or status)


def _human(e: Exception) -> str:
    return _humanize(str(e) or e.__class__.__name__)


def _humanize(text: str) -> str:
    """Operator-facing one-liner. Never a traceback, never a raw provider dump."""
    if "401" in text or "unauthenticated" in text:
        return ("TTN rechazó la petición (401): la API key del backend no es válida "
                "o no tiene permiso de downlink. Revisa TTN_API_KEY en el servidor.")
    if "403" in text or "permission" in text:
        return "TTN rechazó la petición (403): la API key no tiene ese permiso."
    return text[:240]


@bp.errorhandler(Exception)
def _panel_error(e):
    """Last resort: nothing unhandled ever renders as a stack trace."""
    if isinstance(e, HTTPException):
        return e
    current_app.logger.exception("panel error on %s %s", request.method, request.path)
    flash(_human(e), "error")
    dev_eui = (request.view_args or {}).get("dev_eui")
    if request.method == "POST" and dev_eui:
        return redirect(_station_url(dev_eui, request.form.get("tab")))
    if request.endpoint != "web.dashboard":
        return redirect(url_for("web.dashboard"))
    return render_template("base.html"), 500


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
    if u_type == "boot":
        lkg = d.get("lkg_epoch_s")
        if not lkg:
            return "arranque sin hora de referencia"
        return "arranque; última hora fiable " + time.strftime("%Y-%m-%d %H:%M", time.gmtime(lkg)) + " UTC"
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
            flash(f"No se pudo cambiar: {_human(e)}", "error")
    return render_template("password.html")


# --- pages -------------------------------------------------------------------

@bp.get("/")
def dashboard():
    return render_template("dashboard.html", stations=_services().panel.stations())


@bp.route("/stations/new", methods=["GET", "POST"])
def station_new():
    """Register a device from the panel; optionally provision it in TTN (OTAA)."""
    if request.method == "POST":
        f = request.form
        dev_id = f.get("dev_id", "").strip()
        if not dev_id:
            flash("El ID de dispositivo es obligatorio", "error")
            return render_template("station_new.html", timezones=TIMEZONES, form=f)
        keys = {k: f.get(k, "") for k in ("dev_eui", "join_eui", "app_key")}
        ttn_keys = keys if any(v.strip() for v in keys.values()) else None
        try:
            tz = f.get("tz", "UTC")
            _services().panel.add_station(
                dev_id,
                name=f.get("name", "").strip(),
                mode=f.get("mode", "forward"),
                utc_offset_min=_tz_offset_min(tz),
                lat=float(f.get("lat") or 0.0),
                lon=float(f.get("lon") or 0.0),
                ttn_keys=ttn_keys,
            )
        except AppError:
            flash(f"El dispositivo '{dev_id}' ya existe", "error")
            return render_template("station_new.html", timezones=TIMEZONES, form=f)
        except Exception as e:
            flash(f"No se pudo dar de alta: {_human(e)}", "error")
            return render_template("station_new.html", timezones=TIMEZONES, form=f)
        flash("Dispositivo dado de alta" +
              (" y aprovisionado en TTN" if ttn_keys else ""), "ok")
        return redirect(url_for("web.station", dev_eui=dev_id))
    return render_template("station_new.html", timezones=TIMEZONES, form={})


STATION_TABS = ("resumen", "lecturas", "actividad", "ajustes")


def _station_url(dev_eui: str, tab: str | None = None) -> str:
    """Back to the station page, on the tab the action was fired from."""
    return url_for("web.station", dev_eui=dev_eui,
                   tab=tab if tab in STATION_TABS else None)


@bp.get("/stations/<dev_eui>")
def station(dev_eui: str):
    svc = _services()
    tab = request.args.get("tab", "")
    tab = tab if tab in STATION_TABS else STATION_TABS[0]
    st = svc.panel.station(dev_eui)
    ups = svc.panel.uplinks(dev_eui)
    return render_template(
        "station.html",
        st=st,
        station_local_now=_fmt_dt(int(time.time()), st.utc_offset_min),
        timezones=TIMEZONES,
        uplinks=[(u, _summary(u.u_type, u.payload_hex)) for u in ups],
        downlinks=svc.panel.downlinks(dev_eui),
        readings=svc.panel.readings(dev_eui),
        forecast=svc.panel.latest_forecast(dev_eui),
        config_state=svc.panel.config_state(dev_eui),
        tab=tab,
    )


@bp.post("/stations/<dev_eui>/timezone")
def station_timezone(dev_eui: str):
    """Set the station timezone: DST-aware offset via LoRa TLV + DB mirror."""
    tz = request.form.get("tz", "")
    if tz not in {z for _, z in TIMEZONES}:
        flash("Zona horaria no reconocida", "error")
        return redirect(_station_url(dev_eui, request.form.get("tab")))
    offset = _tz_offset_min(tz)
    svc = _services()
    try:
        svc.config_downlink.run(dev_eui, {"utc_offset_min": offset}, int(time.time()))
        svc.panel.update_station(dev_eui, {"utc_offset_min": offset})
        flash(f"Zona horaria {tz} (UTC{offset / 60:+.0f} h) encolada por LoRa; "
              "la estación confirmará con CFG_ACK", "ok")
    except Exception as e:
        flash(f"No se pudo encolar: {_human(e)}", "error")
    return redirect(_station_url(dev_eui, request.form.get("tab")))


# --- actions -----------------------------------------------------------------

# Config fields sent as LoRa TLVs; (form name, cast). Empty inputs are skipped.
_CFG_FIELDS = [
    ("sleep_s", int), ("deep_sleep", int), ("capture_s", int), ("daily_hour", int),
    ("daily_min", int),
    ("lora_period_s", int), ("inference_mode", int), ("utc_offset_min", int),
    ("lat", float), ("lon", float), ("log_level", int),
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
        return redirect(_station_url(dev_eui, request.form.get("tab")))
    if not fields:
        flash("Ningún campo a enviar", "error")
        return redirect(_station_url(dev_eui, request.form.get("tab")))
    try:
        cmd = svc.config_downlink.run(dev_eui, fields, int(time.time()))
    except ValueError as e:
        flash(f"Config rechazada: {e}", "error")
        return redirect(_station_url(dev_eui, request.form.get("tab")))
    except Exception as e:
        flash(f"No se pudo encolar la config en TTN: {_human(e)} — "
              "los valores NO se han guardado ni enviado a la estación", "error")
        return redirect(_station_url(dev_eui, request.form.get("tab")))
    mirror = {k: v for k, v in fields.items() if k in _DB_MIRROR}
    if "inference_mode" in fields:
        mirror["mode"] = "local" if fields["inference_mode"] == 1 else "forward"
    if mirror:
        svc.panel.update_station(dev_eui, mirror)
    flash(f"Config encolada en TTN ({len(cmd.payload)} B, FPort {cmd.f_port}). "
          "La estación aún no la tiene: viaja en su próxima ventana RX y quedará "
          "confirmada cuando responda con CFG_ACK", "ok")
    return redirect(_station_url(dev_eui, request.form.get("tab")))


@bp.post("/stations/<dev_eui>/meta")
def station_meta(dev_eui: str):
    patch = {k: request.form[k] for k in ("name", "mode") if request.form.get(k)}
    if patch:
        try:
            _services().panel.update_station(dev_eui, patch)
        except Exception as e:
            flash(f"No se pudo guardar: {_human(e)}", "error")
            return redirect(_station_url(dev_eui, request.form.get("tab")))
        flash("Guardado en el backend. Ojo: el modo de inferencia del nodo solo "
              "cambia enviándolo por LoRa desde «Configurar por LoRa»", "ok")
    return redirect(_station_url(dev_eui, request.form.get("tab")))


@bp.post("/stations/<dev_eui>/downlink")
def station_downlink(dev_eui: str):
    try:
        cmd = _services().schedule_downlink.run(dev_eui, int(time.time()))
        flash(f"Sincronización hora+TA encolada ({len(cmd.payload)} B)", "ok")
    except Exception as e:   # Open-Meteo/TTN failures surface as flash, not 500
        flash(f"No se pudo encolar: {_human(e)}", "error")
    return redirect(_station_url(dev_eui, request.form.get("tab")))


@bp.post("/stations/<dev_eui>/infer")
def station_infer(dev_eui: str):
    svc = _services()
    try:
        svc.run_inference.run(svc.panel.station(dev_eui), int(time.time()))
        flash("Inferencia ejecutada; resultado almacenado y downlink encolado", "ok")
    except InsufficientData as e:
        flash(f"Datos insuficientes para inferir: {e}", "error")
    except Exception as e:
        flash(f"Inferencia fallida: {_human(e)}", "error")
    return redirect(_station_url(dev_eui, request.form.get("tab")))
