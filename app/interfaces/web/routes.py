"""Web panel (driving adapter): server-rendered operator console.

Authenticates against the same AuthService as the JSON API (operator account
`admin`, seeded in create_app; while it keeps the default password every page
redirects to the password form); the bearer token lives in the Flask session
cookie. Fleet-wide queries go through PanelService.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from werkzeug.exceptions import HTTPException

from ...adapters.ttn import codec
from ...application.errors import (
    AppError,
    InsufficientData,
    NotFound,
    StationClaimed,
    Unauthorized,
)
from ...application.services import DEFAULT_ADMIN_PASSWORD, Services
from ...domain.models import (
    DL_APPLIED,
    DL_DELIVERED,
    DL_DISMISSED,
    DL_FAILED,
    DL_QUEUED,
    DownlinkRecord,
    Station,
    UplinkRecord,
)
from .charts import build_soil_chart, forecast_payload, readings_payload
from .view import fleet_cards, fleet_summary, is_online, latest_values

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


@bp.app_template_filter("ago")
def _ago(ts_s, now_s=None) -> str:
    """Epoch seconds -> 'hace 4 min'. Same wording live.js uses in the timeline."""
    if not ts_s:
        return "—"
    d = max(0, int((time.time() if now_s is None else now_s) - int(ts_s)))
    if d < 60:
        return f"hace {d} s"
    if d < 3600:
        return f"hace {d // 60} min"
    if d < 86400:
        return f"hace {d // 3600} h"
    return f"hace {d // 86400} d"


@bp.app_context_processor
def _nav():
    """Sidebar highlight (every station page lives under 'Estaciones') plus the
    one fleet-wide fact the chrome states: which transport the stations use."""
    endpoint = (request.endpoint or "").removeprefix("web.")
    return {"nav_active": "password" if endpoint == "password" else "stations",
            "link_mode": current_app.config["SETTINGS"].link_mode}


def _current_user():
    """Resolve the session-cookie token, or None when not logged in."""
    try:
        return _services().auth.resolve(session.get("token", ""))
    except Unauthorized:
        return None


@bp.before_request
def _gate():
    """Operator session required; a default password must be replaced first."""
    if request.endpoint in ("web.login", "web.static"):
        return None
    if _current_user() is None:
        if request.endpoint == "web.station_live":   # polled: no HTML login page
            return jsonify(error="Unauthorized", message="session required"), 401
        return redirect(url_for("web.login"))
    if session.get("must_change_pw") and request.endpoint not in ("web.password",
                                                                   "web.logout"):
        flash("Cambia la contraseña por defecto antes de continuar", "error")
        return redirect(url_for("web.password"))
    return None


# Downlink lifecycle as shown in the panel.
_DL_LABEL = {DL_QUEUED: "en cola", DL_DELIVERED: "entregado a la estación",
             DL_APPLIED: "aplicada", DL_FAILED: "no enviada",
             DL_DISMISSED: "no enviada (aviso descartado)"}


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
    if u_type == "ping":
        return "ping de cobertura pedido desde la app (confirmado)"
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


# --- live communication ------------------------------------------------------

LIVE_UPLINKS = 12
LIVE_DOWNLINKS = 8
BANNER_DELIVERED_S = 6     # how long "delivered" stays on screen
BANNER_RESULT_S = 90       # how long the station's forecast stays on screen


@bp.app_template_filter("es_num")
def _es_num(value: float, decimals: int = 3) -> str:
    """Spanish decimal comma: 0.742 -> '0,742'."""
    return f"{value:.{decimals}f}".replace(".", ",")


@bp.app_template_filter("clock")
def _fmt_clock(ts_s: int, offset_min: int = 0) -> str:
    """Epoch seconds -> 'HH:MM:SS' station-local; live.js swaps it for 'hace 4 s'."""
    return time.strftime("%H:%M:%S", time.gmtime(int(ts_s) + offset_min * 60))


HEX_PREVIEW_BYTES = 24


@bp.app_template_filter("hex_bytes")
def _hex_bytes(payload_hex: str) -> str:
    """'02016a45...' -> '02 01 6a 45 …', cut after HEX_PREVIEW_BYTES bytes."""
    pairs = [payload_hex[i:i + 2] for i in range(0, len(payload_hex), 2)]
    more = " …" if len(pairs) > HEX_PREVIEW_BYTES else ""
    return " ".join(pairs[:HEX_PREVIEW_BYTES]) + more


def _dl_summary(kind: str, payload: bytes) -> tuple[str, dict | None]:
    """Human one-liner for a logged downlink + its TA window when it carries one."""
    try:
        if kind == "time_ta":
            d = codec.decode_downlink_time_ta(payload)
            clock = d["clock_epoch_s"]
            hour = time.strftime("%H:%M:%S", time.gmtime(clock)) + " UTC" if clock else "sin hora"
            past, future = d["past_ta"], d["future_ta"]
            if not past and not future:
                return f"sincronización de hora · {hour}", None
            both = past + future
            return (f"hora {hour} + temperatura del aire: {len(past)} h anteriores "
                    f"y {len(future)} h de previsión",
                    {"past": past, "future": future, "min": min(both), "max": max(both)})
        if kind == "config":
            fields = codec.decode_config_patch_tlv(payload)
            return "configuración: " + ", ".join(f"{k} = {v}" for k, v in fields.items()), None
    except ValueError:
        pass
    return "no decodificable", None


def _live_uplink(u: UplinkRecord) -> dict:
    out = {
        "id": u.id, "ts_s": u.ts_s, "type": u.u_type,
        "summary": _summary(u.u_type, u.payload_hex),
        "payload_hex": u.payload_hex, "bytes": len(u.payload_hex) // 2,
    }
    if u.u_type == "forecast":
        try:
            out["hs30_min"] = codec.decode_uplink(bytes.fromhex(u.payload_hex)).get("hs30_min")
        except (ValueError, TypeError):
            out["hs30_min"] = None
    return out


def _live_downlink(d: DownlinkRecord, deliveries: dict[str, int]) -> dict:
    try:
        payload = bytes.fromhex(d.payload_hex)
    except ValueError:
        payload = b""
    summary, ta = _dl_summary(d.kind, payload)
    out = {
        "id": d.id, "ts_s": d.ts_s, "kind": d.kind, "state": d.state,
        "label": _dl_label(d.status), "summary": summary,
        "payload_hex": d.payload_hex, "bytes": len(payload),
        # When the station took it (HTTP link); None while queued or over TTN.
        "delivered_s": (deliveries.get(d.payload_hex)
                        if d.state in (DL_DELIVERED, DL_APPLIED) else None),
    }
    if ta is not None:
        out["ta"] = ta
    return out


def _live_forecast(svc: Services, dev_eui: str, uplinks: list[dict]) -> dict | None:
    """Newest known forecast: a stored cloud run or the value the station reported."""
    best = None
    run = svc.panel.latest_forecast(dev_eui)
    if run is not None and run.hs30:
        best = {"run_ts_s": run.run_ts_s, "hs30_min": min(run.hs30), "source": "cloud"}
    reported = next((u for u in uplinks if u.get("hs30_min") is not None), None)
    if reported and (best is None or reported["ts_s"] >= best["run_ts_s"]):
        best = {"run_ts_s": reported["ts_s"], "hs30_min": reported["hs30_min"],
                "source": "station"}
    return best


def _banner(uplinks: list[dict], downlinks: list[dict], now_s: int) -> dict | None:
    """In-flight notice of the station page (both lists newest first): a queued
    downlink, then its delivery, then the forecast the station sends after inferring."""
    queued = [d for d in downlinks if d["state"] == DL_QUEUED]
    if queued:
        nxt = queued[-1]   # FIFO: the oldest one leaves first
        more = f" · {len(queued) - 1} más en cola" if len(queued) > 1 else ""
        return {"state": "sending", "key": f"d{nxt['id']}",
                "title": "Enviando paquete por LoRa…",
                "detail": "esperando la ventana de recepción de la estación" + more,
                "meta": f"{nxt['kind']} · {nxt['bytes']} B"}
    taken = [d for d in downlinks if d["delivered_s"] is not None]
    last = max(taken, key=lambda d: d["delivered_s"], default=None)
    if last and now_s - last["delivered_s"] <= BANNER_DELIVERED_S:
        return {"state": "delivered", "key": f"d{last['id']}",
                "title": "Paquete entregado a la estación",
                "detail": "recibido en su ventana de recepción",
                "meta": f"{last['kind']} · {last['bytes']} B"}
    with_ta = [d for d in taken if d.get("ta")]
    window = max(with_ta, key=lambda d: d["delivered_s"], default=None)
    if window:
        # Strictly later: the uplink that fetched the window predates the inference.
        first = next((u for u in reversed(uplinks)
                      if u.get("hs30_min") is not None
                      and u["ts_s"] > window["delivered_s"]), None)
        if first and now_s - first["ts_s"] <= BANNER_RESULT_S:
            return {"state": "result", "key": f"u{first['id']}",
                    "title": "La estación ha ejecutado el modelo · HS30 mínimo previsto "
                             + _es_num(first["hs30_min"]),
                    "detail": "pronóstico a 24 h calculado a bordo con la hora y la "
                              "temperatura recibidas",
                    "meta": f"forecast · {first['bytes']} B"}
    return None


def _live(svc: Services, st: Station, now_s: int) -> dict:
    """Everything the live card and the in-flight banner draw (also live.json)."""
    link_mode = current_app.config["SETTINGS"].link_mode
    outbox = svc.link_outbox
    deliveries = outbox.deliveries(st.dev_eui, LIVE_DOWNLINKS * 2) if outbox else {}
    uplinks = [_live_uplink(u) for u in svc.panel.uplinks(st.dev_eui, LIVE_UPLINKS)]
    downlinks = [_live_downlink(d, deliveries)
                 for d in svc.panel.downlinks(st.dev_eui, LIVE_DOWNLINKS)]
    return {
        "now_s": now_s,
        "link_mode": link_mode,
        "pending": outbox.pending(st.dev_eui) if outbox else 0,
        "uplinks": uplinks,
        "downlinks": downlinks,
        "forecast": _live_forecast(svc, st.dev_eui, uplinks),
        "station": {"last_uplink_at": st.last_uplink_at, "mode": st.mode,
                    "utc_offset_min": st.utc_offset_min},
        # Over TTN a delivery is never observed, so the banner only exists on the link.
        "banner": _banner(uplinks, downlinks, now_s) if link_mode == "http" else None,
    }


def _timeline(live: dict) -> list[dict]:
    """Uplinks and downlinks interleaved, newest first. A delivered downlink sorts by
    its delivery time and, on a tie, above the uplink it answered."""
    rows = [{"dir": "up", "key": f"u{u['id']}", "at_s": u["ts_s"], **u}
            for u in live["uplinks"]]
    rows += [{"dir": "down", "key": f"d{d['id']}", "at_s": d["delivered_s"] or d["ts_s"], **d}
             for d in live["downlinks"]]
    rows.sort(key=lambda r: (r["at_s"], r["dir"] == "down", r["id"] or 0), reverse=True)
    return rows


# --- auth --------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        try:
            token = _services().auth.login(request.form.get("user", ""), password,
                                           allow_default=True)
        except AppError:
            flash("Usuario o contraseña incorrectos", "error")
            return render_template("login.html")
        session.clear()
        session["token"] = token
        if password == DEFAULT_ADMIN_PASSWORD:
            session["must_change_pw"] = True
            flash("Estás usando la contraseña por defecto: cámbiala para continuar", "error")
            return redirect(url_for("web.password"))
        return redirect(url_for("web.dashboard"))
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
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
            session.pop("must_change_pw", None)
            flash("Contraseña actualizada", "ok")
            return redirect(url_for("web.dashboard"))
        except AppError as e:
            flash(f"No se pudo cambiar: {_human(e)}", "error")
    return render_template("password.html")


# --- pages -------------------------------------------------------------------

@bp.get("/")
def dashboard():
    now_s = int(time.time())
    cards = fleet_cards(_services().panel, now_s)
    return render_template("dashboard.html", cards=cards,
                           fleet=fleet_summary(cards), now_s=now_s)


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
        except StationClaimed:
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
    now_s = int(time.time())
    st = svc.panel.station(dev_eui)
    ups = svc.panel.uplinks(dev_eui)
    live = _live(svc, st, now_s)
    readings = svc.panel.readings(dev_eui)
    forecast = svc.panel.latest_forecast(dev_eui)
    return render_template(
        "station.html",
        st=st,
        live=live,
        timeline=_timeline(live),
        station_local_now=_fmt_dt(now_s, st.utc_offset_min),
        timezones=TIMEZONES,
        uplinks=[(u, _summary(u.u_type, u.payload_hex)) for u in ups],
        downlinks=svc.panel.downlinks(dev_eui),
        readings=readings,
        soil_chart=build_soil_chart(readings, st.utc_offset_min),
        readings_json=readings_payload(readings, st.utc_offset_min),
        forecast=forecast,
        forecast_json=forecast_payload(forecast, st.utc_offset_min),
        latest=latest_values(readings),
        online=is_online(st, now_s),
        now_s=now_s,
        config_state=svc.panel.config_state(dev_eui),
        tab=tab,
    )


@bp.get("/stations/<dev_eui>/live.json")
def station_live(dev_eui: str):
    """Polled by live.js: latest frames both ways + the in-flight banner."""
    svc = _services()
    try:
        st = svc.panel.station(dev_eui)
    except NotFound:
        return jsonify(error="NotFound", message="unknown station"), 404
    resp = jsonify(_live(svc, st, int(time.time())))
    resp.headers["Cache-Control"] = "no-store"
    return resp


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


@bp.post("/stations/<dev_eui>/config-state/dismiss")
def station_config_dismiss(dev_eui: str):
    """Put away the configuration notice. The downlink log keeps the row and its
    detail; what goes is the banner sitting on top of the station page."""
    if _services().panel.dismiss_config_notice(dev_eui):
        flash("Aviso descartado; queda en el registro de downlinks", "ok")
    else:
        flash("No hay ningún aviso de configuración que descartar", "error")
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
