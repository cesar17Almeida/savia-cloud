"""Use cases: orchestrate domain + ports. No framework, no I/O details."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from ..adapters.ttn import codec
from ..domain.models import (
    DL_APPLIED,
    DL_CANCELLED,
    DL_DELIVERED,
    DL_DISMISSED,
    DL_FAILED,
    DL_QUEUED,
    DownlinkCommand,
    DownlinkRecord,
    Forecast,
    ForecastRun,
    QueuedDownlink,
    Session,
    SoilReading,
    Station,
    UplinkRecord,
    User,
)
from ..domain.ports import (
    DownlinkLogRepository,
    ForecastPort,
    ForecastRepository,
    InferencePort,
    LinkOutboxRepository,
    ReadingRepository,
    SessionRepository,
    StationRepository,
    TtnPort,
    UplinkLogRepository,
    UserRepository,
)
from .errors import (
    EmailTaken,
    Forbidden,
    InsufficientData,
    InvalidCredentials,
    InvalidInput,
    NotFound,
    PasswordChangeRequired,
    RegistrationClosed,
    StationClaimed,
    Unauthorized,
)

# FPort the station uses (mirror savia_c LORA_FPORT).
FPORT = 8

HOUR_S = 3600
PAST_STEPS = 48
FUTURE_STEPS = 24
# Admission guards for the LSTM window. The firmware applies the SAME THREE guards
# (savia_c lstm_input.h) so a window is judged by one policy wherever it runs. Two of
# the thresholds are identical on both sides; MAX_NEWEST_AGE_H is deliberately looser
# here: the station samples right before inferring and can demand a fresh bucket,
# while readings reach the backend by radio, late and with a backlog, so requiring a
# real reading in the very hour being inferred would stop FORWARD mode almost always.
MAX_SOIL_GAP_H = 6              # >6 contiguous missing soil hours -> refuse
MIN_REAL_HOURS = 24             # real hourly buckets needed inside the 48 h window
MAX_NEWEST_AGE_H = 2            # how old the newest REAL bucket may be (firmware: 0)
SESSION_TTL_S = 30 * 86400     # bearer token lifetime: 30 days
STATION_MODES = ("forward", "local")
# Seeded operator password: the panel forces a change, the JSON API refuses it.
DEFAULT_ADMIN_PASSWORD = "admin"
MIN_PASSWORD_LEN = 8
# Plausible soil timestamps; anything else is junk that would also overflow int32.
TS_MIN_S = 1_577_836_800          # 2020-01-01T00:00Z
TS_FUTURE_SLACK_S = 86400         # tolerate a day of clock skew ahead of the backend


def _now_s() -> int:
    return int(time.time())


# --- LSTM window builder (pure) ----------------------------------------------

def _locf(values: list[float | None]) -> list[float]:
    """Fill gaps last-observation-carried-forward; leading gap back-filled from the
    first known sample. Raises InsufficientData if the series is entirely empty."""
    first = next((i for i, v in enumerate(values) if v is not None), None)
    if first is None:
        raise InsufficientData("soil series has no data in the window")
    out = [values[first]] * (first + 1)
    for v in values[first + 1:]:
        out.append(out[-1] if v is None else v)
    return out


def build_lstm_window(
    readings: list[SoilReading],
    forecast: Forecast,
    now_s: int,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Assemble (ta, hs10, hs30, future_ta) for the 48 h window ending at the hour of
    now_s. TA gaps are filled from the Open-Meteo past and soil gaps LOCF, but only
    after the window clears the three admission guards (coverage, freshness,
    continuity); any of them failing raises InsufficientData."""
    latest_hour = now_s - (now_s % HOUR_S)
    hours = [latest_hour - (PAST_STEPS - 1 - i) * HOUR_S for i in range(PAST_STEPS)]
    by_hour = {r.ts_hour_s: r for r in readings}

    ta_raw: list[float | None] = []
    hs10_raw: list[float | None] = []
    hs30_raw: list[float | None] = []
    soil_present: list[bool] = []
    for i, h in enumerate(hours):
        r = by_hour.get(h)
        hs10_raw.append(r.hs10 if r else None)
        hs30_raw.append(r.hs30 if r else None)
        soil_present.append(bool(r and (r.hs10 is not None or r.hs30 is not None)))
        # TA prefers the station's own reading, else the Open-Meteo past bucket.
        if r and r.ta is not None:
            ta_raw.append(r.ta)
        else:
            ta_raw.append(forecast.past_ta[i] if i < len(forecast.past_ta) else None)

    # Guard 1 -- coverage: LOCF is meant to bridge a missed hour, not to manufacture
    # two days of history out of a handful of samples.
    real = sum(soil_present)
    if real < MIN_REAL_HOURS:
        raise InsufficientData(
            f"only {real} real soil hours in the window, {MIN_REAL_HOURS} needed"
        )

    # Guard 2 -- freshness: a copy in the newest buckets forecasts from soil that may
    # no longer exist (a shower or an irrigation inside the copied span is invisible).
    newest_age = next((i for i, p in enumerate(reversed(soil_present)) if p), PAST_STEPS)
    if newest_age > MAX_NEWEST_AGE_H:
        raise InsufficientData(
            f"newest real soil reading is {newest_age} h old, max {MAX_NEWEST_AGE_H} h"
        )

    # Guard 3 -- continuity: the longer a copied span, the likelier it hides a
    # discrete event the model never sees.
    gap = worst = 0
    for present in soil_present:
        gap = 0 if present else gap + 1
        worst = max(worst, gap)
    if worst > MAX_SOIL_GAP_H:
        raise InsufficientData(f"soil gap of {worst} h exceeds {MAX_SOIL_GAP_H} h")

    ta = _locf(ta_raw)
    hs10 = _locf(hs10_raw)
    hs30 = _locf(hs30_raw)
    future_ta = list(forecast.future_ta[:FUTURE_STEPS])
    return ta, hs10, hs30, future_ta


# --- auth --------------------------------------------------------------------

class AuthService:
    """Register / login and resolve bearer tokens. Password hashing + the clock are
    injected so the core stays free of werkzeug and wall-clock coupling."""

    def __init__(
        self,
        users: UserRepository,
        sessions: SessionRepository,
        hash_pw: Callable[[str], str],
        verify_pw: Callable[[str, str], bool],
        clock: Callable[[], int] = _now_s,
        allow_registration: bool = True,
    ):
        self._users = users
        self._sessions = sessions
        self._hash = hash_pw
        self._verify = verify_pw
        self._clock = clock
        self._allow_registration = allow_registration

    def register(self, email: str, password: str) -> User:
        if not self._allow_registration:
            raise RegistrationClosed("self-registration is disabled on this server")
        if not email or not password:
            raise InvalidCredentials("email and password required")
        if self._users.get_by_email(email):
            raise EmailTaken(email)
        return self._users.add(email, self._hash(password))

    def login(self, email: str, password: str, allow_default: bool = False) -> str:
        """Issue a session token; the default password is only accepted by the panel."""
        user = self._users.get_by_email(email)
        if not user or not self._verify(user.pw_hash, password):
            raise InvalidCredentials("bad email or password")
        if password == DEFAULT_ADMIN_PASSWORD and not allow_default:
            raise PasswordChangeRequired("change the default password in the web panel first")
        token = secrets.token_urlsafe(32)
        self._sessions.create(Session(token, user.id, self._clock() + SESSION_TTL_S))
        return token

    def resolve(self, token: str) -> User:
        sess = self._sessions.get(token) if token else None
        if not sess or sess.expires_at < self._clock():
            raise Unauthorized("invalid or expired session")
        user = self._users.get_by_id(sess.user_id)
        if not user:
            raise Unauthorized("session user not found")
        return user

    def change_password(self, user: User, old_password: str, new_password: str) -> None:
        if not new_password:
            raise InvalidCredentials("new password required")
        if new_password == DEFAULT_ADMIN_PASSWORD or len(new_password) < MIN_PASSWORD_LEN:
            raise InvalidInput(f"the new password needs at least {MIN_PASSWORD_LEN} "
                               "characters and cannot be the default one")
        if not self._verify(user.pw_hash, old_password):
            raise InvalidCredentials("current password is wrong")
        self._users.update_password(user.id, self._hash(new_password))


# --- station ownership -------------------------------------------------------

def _check_mode(mode: str) -> str:
    if mode not in STATION_MODES:
        raise InvalidInput(f"mode must be one of {', '.join(STATION_MODES)}")
    return mode


def _apply_station_patch(st: Station, patch: dict) -> None:
    """Apply an API/panel patch in place; bad values raise InvalidInput (400)."""
    if "name" in patch:
        st.name = str(patch["name"])
    if "mode" in patch:
        st.mode = _check_mode(str(patch["mode"]))
    try:
        for key in ("lat", "lon"):
            if key in patch:
                setattr(st, key, float(patch[key]))
        if "utc_offset_min" in patch:
            st.utc_offset_min = int(patch["utc_offset_min"])
    except (TypeError, ValueError, OverflowError):
        raise InvalidInput("lat/lon must be numbers and utc_offset_min an integer") from None


class StationService:
    """Claim + owner-scoped read/update of stations."""

    def __init__(self, stations: StationRepository):
        self._stations = stations

    def claim(self, user_id: int, dev_eui: str, name: str | None) -> Station:
        st = self._stations.get(dev_eui)
        if st is None:
            st = Station(dev_eui=dev_eui, user_id=user_id, name=name or dev_eui)
        elif st.user_id not in (None, user_id):
            raise StationClaimed(dev_eui)
        else:
            st.user_id = user_id
            if name:
                st.name = name
        self._stations.save(st)
        return st

    def get_owned(self, user_id: int, dev_eui: str) -> Station:
        st = self._stations.get(dev_eui)
        if st is None:
            raise NotFound(dev_eui)
        if st.user_id != user_id:
            raise Forbidden(dev_eui)
        return st

    def update(self, user_id: int, dev_eui: str, patch: dict) -> Station:
        st = self.get_owned(user_id, dev_eui)
        _apply_station_patch(st, patch)
        self._stations.save(st)
        return st


# --- uplink ingestion --------------------------------------------------------

# Re-sync the station clock at most this often (TTN fair use: <=10 downlinks/day).
TIME_SYNC_GAP_S = 6 * 3600


def _log_type(decoded: dict, confirmed: bool) -> str:
    """Label for the raw log. A CONFIRMED forecast frame carrying no value is the
    on-demand coverage ping the app triggers: the periodic cycle always sends
    unconfirmed frames, so the confirmation bit is what tells the two apart. Only
    the label changes -- everything downstream still treats it as a forecast."""
    u_type = decoded.get("type", "unknown")
    if confirmed and u_type == "forecast" and decoded.get("hs30_min") is None:
        return "ping"
    return u_type


class IngestUplinkService:
    """Persist a decoded uplink: raw log + soil records + coords + link quality.
    Also keeps the station clock fresh: if no time_ta downlink went out in the
    last TIME_SYNC_GAP_S, queue a pure 8-byte clock sync for the next RX window.
    A BOOT uplink (the node's first frame after power-up) queues it unconditionally:
    the node has no clock and that RX window is its first chance to get one."""

    def __init__(
        self,
        stations: StationRepository,
        readings: ReadingRepository,
        uplinks: UplinkLogRepository | None = None,
        default_lat: float = 0.0,
        default_lon: float = 0.0,
        ttn: TtnPort | None = None,
        downlinks: DownlinkLogRepository | None = None,
    ):
        self._stations = stations
        self._readings = readings
        self._uplinks = uplinks
        self._lat = default_lat
        self._lon = default_lon
        self._ttn = ttn
        self._downlinks = downlinks

    def handle(self, dev_eui: str, decoded: dict, rssi, snr, at_s: int,
               raw_hex: str = "", confirmed: bool = False) -> None:
        if self._uplinks is not None:
            self._uplinks.add(UplinkRecord(
                dev_eui=dev_eui, ts_s=at_s, u_type=_log_type(decoded, confirmed),
                payload_hex=raw_hex, rssi=rssi, snr=snr,
            ))
        st = self._stations.get(dev_eui) or Station(
            dev_eui=dev_eui, lat=self._lat, lon=self._lon
        )
        st.last_rssi = rssi
        st.last_snr = snr
        st.last_uplink_at = at_s

        kind = decoded.get("type")
        if kind == "soil":
            for rec in decoded["records"]:
                if not (TS_MIN_S <= rec["ts_hour_s"] <= at_s + TS_FUTURE_SLACK_S):
                    continue   # clockless or corrupted bucket: logged raw, not stored
                self._readings.upsert_soil(SoilReading(
                    dev_eui=dev_eui,
                    ts_hour_s=rec["ts_hour_s"],
                    hs10=rec["hs10"],
                    hs30=rec["hs30"],
                    ta=rec["ta"],
                ))
        elif kind == "coords":
            st.lat = decoded["lat"]
            st.lon = decoded["lon"]
            st.utc_offset_min = decoded["utc_offset_min"]
        elif kind == "cfg_ack" and self._downlinks is not None:
            self._downlinks.confirm_oldest_queued(
                dev_eui, "config", "{}: {} aplicados, {} rechazados".format(
                    DL_APPLIED, decoded.get("applied", 0), decoded.get("rejected", 0)))
        # forecast / boot: only the link-quality + last_uplink_at update above.
        self._stations.save(st)
        self._maybe_queue_clock_sync(dev_eui, at_s, force=(kind == "boot"))

    def _maybe_queue_clock_sync(self, dev_eui: str, at_s: int, force: bool = False) -> None:
        """Keep the station clock fresh without violating TTN fair use. `force`
        (a BOOT frame) skips the gap check: the node just powered up clockless."""
        if self._ttn is None or self._downlinks is None:
            return
        # Failed pushes do not count, so the next uplink retries the sync.
        recent = self._downlinks.list_recent(dev_eui, 5, kind="time_ta")
        last = next((d.ts_s for d in recent if d.state != DL_FAILED), None)
        if not force and last is not None and at_s - last < TIME_SYNC_GAP_S:
            return
        payload = codec.encode_downlink_time_ta([], [], at_s)   # 8 B pure clock
        cmd = DownlinkCommand(dev_id=dev_eui, f_port=FPORT, payload=payload)
        try:
            self._ttn.schedule_downlink(cmd)
            status = DL_QUEUED
        except Exception as e:
            status = f"{DL_FAILED}: {e}"[:200]
        self._downlinks.add(dev_eui, at_s, "time_ta", payload.hex(), status)


# The station keeps its RX window open for 8 s; the same frame again inside this
# span is the phone retrying a POST whose answer got lost, not a new uplink.
LINK_RETRY_WINDOW_S = 10
UTC_OFFSET_MIN_RANGE = (-720, 840)


class LinkUplinkService:
    """HTTP link (LINK_MODE=http): ingest one tunnelled uplink and hand back the
    oldest queued downlink. Class-A semantics: at most one downlink, and only as the
    answer to an uplink."""

    def __init__(
        self,
        ingest: IngestUplinkService,
        stations: StationRepository,
        outbox: LinkOutboxRepository,
        downlinks: DownlinkLogRepository,
    ):
        self._ingest = ingest
        self._stations = stations
        self._outbox = outbox
        self._downlinks = downlinks
        # dev_id -> (seq, raw_hex, at_s, answer) of the last uplink handled.
        self._last: dict[str, tuple] = {}

    def handle(self, dev_id: str, decoded: dict, at_s: int, raw_hex: str = "",
               seq: int | None = None, utc_offset_min: int | None = None,
               confirmed: bool = False) -> DownlinkCommand | None:
        last = self._last.get(dev_id)
        if (seq is not None and last is not None and last[:2] == (seq, raw_hex)
                and 0 <= at_s - last[2] <= LINK_RETRY_WINDOW_S):
            return last[3]   # retry: same answer, nothing ingested twice

        self._ingest.handle(dev_id, decoded, None, None, at_s, raw_hex=raw_hex,
                            confirmed=confirmed)
        if utc_offset_min is not None:
            self._store_offset(dev_id, utc_offset_min)
        self._mirror_mode(dev_id, decoded)

        cmd = self._outbox.take_next(dev_id, at_s)
        if cmd is not None:
            self._downlinks.mark_delivered(dev_id, cmd.payload.hex(), DL_DELIVERED)
        self._last[dev_id] = (seq, raw_hex, at_s, cmd)
        return cmd

    def _mirror_mode(self, dev_id: str, decoded: dict) -> None:
        """Follow the mode the installer set over BLE: only a LOCAL node reports a
        forecast value, only a FORWARD node uplinks soil records."""
        if decoded.get("type") == "soil":
            mode = "forward"
        elif decoded.get("type") == "forecast" and decoded.get("hs30_min") is not None:
            mode = "local"
        else:
            return
        st = self._stations.get(dev_id)
        if st is not None and st.mode != mode:
            st.mode = mode
            self._stations.save(st)

    def _store_offset(self, dev_id: str, utc_offset_min: int) -> None:
        """Mirror the station's UTC offset, exactly as a COORDS uplink would."""
        lo, hi = UTC_OFFSET_MIN_RANGE
        st = self._stations.get(dev_id)
        if st is None or not lo <= utc_offset_min <= hi:
            return
        if st.utc_offset_min != utc_offset_min:
            st.utc_offset_min = utc_offset_min
            self._stations.save(st)


class SignalQueryService:
    """Read the last known uplink signal (RSSI/SNR from the TTN gateway)."""

    def __init__(self, stations: StationRepository):
        self._stations = stations

    def last_signal(self, dev_eui: str) -> dict | None:
        st = self._stations.get(dev_eui)
        if not st or st.last_uplink_at is None:
            return None
        return {"rssi_dbm": st.last_rssi, "snr_db": st.last_snr, "at_s": st.last_uplink_at}


# --- downlinks ---------------------------------------------------------------

class ScheduleDownlinkService:
    """Build + schedule the clock + TA-forecast (TIME_TA) downlink for a station."""

    def __init__(
        self,
        stations: StationRepository,
        forecast: ForecastPort,
        ttn: TtnPort,
        log: DownlinkLogRepository,
        default_lat: float = 0.0,
        default_lon: float = 0.0,
    ):
        self._stations = stations
        self._forecast = forecast
        self._ttn = ttn
        self._log = log
        self._lat = default_lat
        self._lon = default_lon

    def run(self, dev_eui: str, now_s: int) -> DownlinkCommand:
        st = self._stations.get(dev_eui)
        lat = st.lat if st else self._lat
        lon = st.lon if st else self._lon
        fc = self._forecast.fetch(lat, lon)
        payload = codec.encode_downlink_time_ta(fc.past_ta, fc.future_ta, now_s)
        cmd = DownlinkCommand(dev_id=dev_eui, f_port=FPORT, payload=payload)
        try:
            self._ttn.schedule_downlink(cmd)
        except Exception as e:
            self._log.add(dev_eui, now_s, "time_ta", payload.hex(), f"{DL_FAILED}: {e}"[:200])
            raise
        self._log.add(dev_eui, now_s, "time_ta", payload.hex(), DL_QUEUED)
        return cmd


class ConfigDownlinkService:
    """Encode a config-patch TLV and schedule it as a downlink."""

    def __init__(self, ttn: TtnPort, log: DownlinkLogRepository):
        self._ttn = ttn
        self._log = log

    def run(self, dev_eui: str, fields: dict, now_s: int) -> DownlinkCommand:
        payload = codec.encode_config_patch_tlv(fields)   # validates ranges, may raise
        cmd = DownlinkCommand(dev_id=dev_eui, f_port=FPORT, payload=payload)
        try:
            self._ttn.schedule_downlink(cmd)
        except Exception as e:
            self._log.add(dev_eui, now_s, "config", payload.hex(), f"{DL_FAILED}: {e}"[:200])
            raise
        self._log.add(dev_eui, now_s, "config", payload.hex(), DL_QUEUED)
        return cmd


# --- cloud FORWARD inference -------------------------------------------------

class RunCloudInferenceService:
    """FORWARD mode: build the 48 h window, run the LSTM in the cloud, store the 24 h
    HS30 forecast, and push the clock + TA window back to the station."""

    def __init__(
        self,
        readings: ReadingRepository,
        forecasts: ForecastRepository,
        forecast_src: ForecastPort,
        infer: InferencePort,
        ttn: TtnPort,
        log: DownlinkLogRepository,
    ):
        self._readings = readings
        self._forecasts = forecasts
        self._forecast_src = forecast_src
        self._infer = infer
        self._ttn = ttn
        self._log = log

    def run(self, station: Station, now_s: int) -> list[float]:
        latest_hour = now_s - (now_s % HOUR_S)
        from_ts = latest_hour - (PAST_STEPS - 1) * HOUR_S
        readings = self._readings.window(station.dev_eui, from_ts, latest_hour)
        fc = self._forecast_src.fetch(station.lat, station.lon)
        ta, hs10, hs30, future_ta = build_lstm_window(readings, fc, now_s)

        pred = self._infer.predict_hs30(ta, hs10, hs30, future_ta)
        self._forecasts.add_run(station.dev_eui, now_s, pred)

        # The stored inference must survive a TTN outage: log the downlink as
        # failed instead of losing the whole run.
        payload = codec.encode_downlink_time_ta(ta, future_ta, now_s)
        cmd = DownlinkCommand(dev_id=station.dev_eui, f_port=FPORT, payload=payload)
        try:
            self._ttn.schedule_downlink(cmd)
            status = "scheduled"
        except Exception as e:
            status = f"failed: {e}"[:120]
        self._log.add(station.dev_eui, now_s, "time_ta", payload.hex(), status)
        return pred


@dataclass
class CronReport:
    """Outcome of one cron tick, per station."""
    ran: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # dev_eui -> why
    failed: dict[str, str] = field(default_factory=dict)    # dev_eui -> error


class DailyCronService:
    """Run FORWARD inference at each station's local daily hour, once per slot."""

    def __init__(
        self,
        stations: StationRepository,
        run_inference: RunCloudInferenceService,
        daily_hour: int,
        forecasts: ForecastRepository | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self._stations = stations
        self._run = run_inference
        self._daily_hour = daily_hour
        self._forecasts = forecasts
        self._log = log or (lambda msg: None)

    def _ran_this_slot(self, st: Station, now_s: int) -> bool:
        """True when a run is already stored for this station's current local hour."""
        last = self._forecasts.latest_run(st.dev_eui) if self._forecasts else None
        if last is None:
            return False
        offset = st.utc_offset_min * 60
        return (last.run_ts_s + offset) // HOUR_S == (now_s + offset) // HOUR_S

    def run(self, now_s: int, force: bool = False) -> CronReport:
        report = CronReport()
        for st in self._stations.list_by_mode("forward"):
            local_hour = ((now_s + st.utc_offset_min * 60) // HOUR_S) % 24
            if not force and local_hour != self._daily_hour:
                continue
            if not force and self._ran_this_slot(st, now_s):
                report.skipped[st.dev_eui] = "already ran in this daily slot"
                continue
            try:
                self._run.run(st, now_s)
                report.ran.append(st.dev_eui)
            except InsufficientData as e:
                report.skipped[st.dev_eui] = str(e)
            except Exception as e:   # one station's failure never stops the rest
                report.failed[st.dev_eui] = f"{type(e).__name__}: {e}"[:200]
                self._log(f"daily inference failed for {st.dev_eui}: {e}")
        return report


class LinkQueueService:
    """Operator control over the HTTP-link outbox: what is still waiting, holding a
    station's queue, and pulling one frame out before the station ever hears it.

    Only the HTTP link has a queue the backend owns. Over TTN the frames live in
    the Things Stack queue, so this service is simply not wired (see create_app).
    """

    def __init__(self, outbox: LinkOutboxRepository, downlinks: DownlinkLogRepository,
                 stations: StationRepository):
        self._outbox = outbox
        self._downlinks = downlinks
        self._stations = stations

    def pending(self, dev_eui: str | None = None) -> list[QueuedDownlink]:
        """Everything still queued, oldest first; the whole fleet when dev_eui is None."""
        return self._outbox.list_pending(dev_eui)

    def paused(self) -> dict[str, int]:
        return self._outbox.paused()

    def stations(self) -> list[Station]:
        return self._stations.list_all()

    def set_paused(self, dev_eui: str, paused: bool, now_s: int) -> Station:
        """Hold or release one station's queue. The station must exist, so a typo in
        the URL cannot create a hold nobody can find again."""
        st = self._stations.get(dev_eui)
        if st is None:
            raise NotFound(dev_eui)
        self._outbox.set_paused(dev_eui, paused, now_s)
        return st

    def cancel(self, item_id: int) -> QueuedDownlink | None:
        """Pull one frame out of the queue. The entry goes and the logged downlink
        moves to `cancelled`, so the history still says the frame existed and why it
        never left. None when it was already delivered or is no longer there."""
        gone = self._outbox.remove(item_id)
        if gone is None:
            return None
        self._downlinks.mark_delivered(
            gone.dev_id, gone.payload_hex,
            f"{DL_CANCELLED}: retirado de la cola por el operador")
        return gone


class PanelService:
    """Operator-console queries: every station, its traffic and stored data.
    Unlike StationService this is NOT owner-scoped -- the web panel authenticates
    as the operator (admin) and oversees the whole fleet."""

    def __init__(
        self,
        stations: StationRepository,
        readings: ReadingRepository,
        forecasts: ForecastRepository,
        uplinks: UplinkLogRepository,
        downlinks: DownlinkLogRepository,
        ttn: TtnPort | None = None,
    ):
        self._stations = stations
        self._readings = readings
        self._forecasts = forecasts
        self._uplinks = uplinks
        self._downlinks = downlinks
        self._ttn = ttn

    def stations(self) -> list[Station]:
        return self._stations.list_all()

    def station(self, dev_eui: str) -> Station:
        st = self._stations.get(dev_eui)
        if st is None:
            raise NotFound(dev_eui)
        return st

    def uplinks(self, dev_eui: str, limit: int = 25) -> list[UplinkRecord]:
        return self._uplinks.list_recent(dev_eui, limit)

    def downlinks(self, dev_eui: str, limit: int = 25) -> list[DownlinkRecord]:
        return self._downlinks.list_recent(dev_eui, limit)

    def readings(self, dev_eui: str, limit: int = 48) -> list[SoilReading]:
        return self._readings.recent(dev_eui, limit)

    def latest_forecast(self, dev_eui: str) -> ForecastRun | None:
        return self._forecasts.latest_run(dev_eui)

    def config_state(self, dev_eui: str) -> dict | None:
        """Newest config downlink as {state, ts_s, detail}; None if none was ever sent."""
        for d in self._downlinks.list_recent(dev_eui, 1, kind="config"):
            return {"state": d.state, "ts_s": d.ts_s, "detail": d.status}
        return None

    def dismiss_config_notice(self, dev_eui: str) -> bool:
        """Put away the config banner of the newest config downlink. The row keeps its
        detail under a `dismissed` token, so the log still says what happened; only an
        unresolved notice (queued, delivered or failed) can be dismissed."""
        for d in self._downlinks.list_recent(dev_eui, 1, kind="config"):
            if d.id is None or d.state not in (DL_QUEUED, DL_DELIVERED, DL_FAILED):
                return False
            _, _, rest = d.status.partition(":")
            return self._downlinks.set_status(
                d.id, f"{DL_DISMISSED}: {rest.strip() or d.state}")
        return False

    def update_station(self, dev_eui: str, patch: dict) -> Station:
        st = self.station(dev_eui)
        _apply_station_patch(st, patch)
        self._stations.save(st)
        return st

    def add_station(
        self,
        dev_eui: str,
        name: str = "",
        mode: str = "forward",
        utc_offset_min: int = 0,
        lat: float = 0.0,
        lon: float = 0.0,
        ttn_keys: dict | None = None,
    ) -> Station:
        """Register a device ahead of its first uplink. With ttn_keys
        {dev_eui, join_eui, app_key} the OTAA device is provisioned in TTN first;
        nothing is stored locally if that provisioning fails."""
        if self._stations.get(dev_eui):
            raise StationClaimed(dev_eui)
        _check_mode(mode)
        if ttn_keys:
            if self._ttn is None:
                raise ValueError("TTN provisioning not available")
            eui = ttn_keys.get("dev_eui", "").strip().upper()
            join = ttn_keys.get("join_eui", "").strip().upper()
            key = ttn_keys.get("app_key", "").strip().upper()
            for label, value, digits in (("DevEUI", eui, 16), ("JoinEUI", join, 16),
                                         ("AppKey", key, 32)):
                if len(value) != digits or any(c not in "0123456789ABCDEF" for c in value):
                    raise ValueError(f"{label} must be {digits} hex digits")
            self._ttn.register_device(dev_eui, eui, join, key)
        st = Station(dev_eui=dev_eui, name=name or dev_eui, mode=mode,
                     utc_offset_min=utc_offset_min, lat=lat, lon=lon)
        self._stations.save(st)
        return st


@dataclass
class Services:
    """Container the HTTP layer reads from app.config["SERVICES"]."""

    auth: AuthService
    stations: StationService
    ingest_uplink: IngestUplinkService
    signal_query: SignalQueryService
    schedule_downlink: ScheduleDownlinkService
    config_downlink: ConfigDownlinkService
    run_inference: RunCloudInferenceService
    daily_cron: DailyCronService
    panel: PanelService
    # HTTP link only (LINK_MODE=http); None when the stations talk through TTN.
    link_uplink: LinkUplinkService | None = None
    link_outbox: LinkOutboxRepository | None = None
    link_queue: LinkQueueService | None = None
