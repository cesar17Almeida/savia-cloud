"""Use cases: orchestrate domain + ports. No framework, no I/O details."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Callable

from ..adapters.ttn import codec
from ..domain.models import (
    DownlinkCommand,
    DownlinkRecord,
    Forecast,
    ForecastRun,
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
    NotFound,
    StationClaimed,
    Unauthorized,
)

# FPort the station uses (mirror savia_c LORA_FPORT).
FPORT = 8

HOUR_S = 3600
PAST_STEPS = 48
FUTURE_STEPS = 24
MAX_SOIL_GAP_H = 6              # >6 contiguous missing soil hours -> refuse
SESSION_TTL_S = 30 * 86400     # bearer token lifetime: 30 days


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
    """Assemble (ta, hs10, hs30, future_ta) for the 48 h window ending at the hour
    of now_s. TA gaps are filled from the Open-Meteo past; soil gaps LOCF; a
    contiguous soil gap longer than MAX_SOIL_GAP_H raises InsufficientData."""
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
    ):
        self._users = users
        self._sessions = sessions
        self._hash = hash_pw
        self._verify = verify_pw
        self._clock = clock

    def register(self, email: str, password: str) -> User:
        if not email or not password:
            raise InvalidCredentials("email and password required")
        if self._users.get_by_email(email):
            raise EmailTaken(email)
        return self._users.add(email, self._hash(password))

    def login(self, email: str, password: str) -> str:
        user = self._users.get_by_email(email)
        if not user or not self._verify(user.pw_hash, password):
            raise InvalidCredentials("bad email or password")
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
        if not self._verify(user.pw_hash, old_password):
            raise InvalidCredentials("current password is wrong")
        self._users.update_password(user.id, self._hash(new_password))


# --- station ownership -------------------------------------------------------

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
        for key in ("name", "mode"):
            if key in patch:
                st.__setattr__(key, str(patch[key]))
        for key in ("lat", "lon"):
            if key in patch:
                st.__setattr__(key, float(patch[key]))
        if "utc_offset_min" in patch:
            st.utc_offset_min = int(patch["utc_offset_min"])
        self._stations.save(st)
        return st


# --- uplink ingestion --------------------------------------------------------

# Re-sync the station clock at most this often (TTN fair use: <=10 downlinks/day).
TIME_SYNC_GAP_S = 6 * 3600


class IngestUplinkService:
    """Persist a decoded uplink: raw log + soil records + coords + link quality.
    Also keeps the station clock fresh: if no time_ta downlink went out in the
    last TIME_SYNC_GAP_S, queue a pure 8-byte clock sync for the next RX window."""

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
               raw_hex: str = "") -> None:
        if self._uplinks is not None:
            self._uplinks.add(UplinkRecord(
                dev_eui=dev_eui, ts_s=at_s, u_type=decoded.get("type", "unknown"),
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
        # forecast / cfg_ack: only the link-quality + last_uplink_at update above.
        self._stations.save(st)
        self._maybe_queue_clock_sync(dev_eui, at_s)

    def _maybe_queue_clock_sync(self, dev_eui: str, at_s: int) -> None:
        """Keep the station clock fresh without violating TTN fair use."""
        if self._ttn is None or self._downlinks is None:
            return
        recent = self._downlinks.list_recent(dev_eui, 10)
        last = next((d.ts_s for d in recent if d.kind == "time_ta"), None)
        if last is not None and at_s - last < TIME_SYNC_GAP_S:
            return
        payload = codec.encode_downlink_time_ta([], [], at_s)   # 8 B pure clock
        cmd = DownlinkCommand(dev_id=dev_eui, f_port=FPORT, payload=payload)
        try:
            self._ttn.schedule_downlink(cmd)
            status = "scheduled"
        except Exception as e:
            status = f"failed: {e}"[:120]
        self._downlinks.add(dev_eui, at_s, "time_ta", payload.hex(), status)


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
        self._ttn.schedule_downlink(cmd)
        self._log.add(dev_eui, now_s, "time_ta", payload.hex(), "scheduled")
        return cmd


class ConfigDownlinkService:
    """Encode a config-patch TLV and schedule it as a downlink."""

    def __init__(self, ttn: TtnPort, log: DownlinkLogRepository):
        self._ttn = ttn
        self._log = log

    def run(self, dev_eui: str, fields: dict, now_s: int) -> DownlinkCommand:
        payload = codec.encode_config_patch_tlv(fields)   # validates ranges, may raise
        cmd = DownlinkCommand(dev_id=dev_eui, f_port=FPORT, payload=payload)
        self._ttn.schedule_downlink(cmd)
        self._log.add(dev_eui, now_s, "config", payload.hex(), "scheduled")
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


class DailyCronService:
    """Run FORWARD inference for the stations whose LOCAL hour matches daily_hour."""

    def __init__(
        self,
        stations: StationRepository,
        run_inference: RunCloudInferenceService,
        daily_hour: int,
    ):
        self._stations = stations
        self._run = run_inference
        self._daily_hour = daily_hour

    def run(self, now_s: int, force: bool = False) -> list[str]:
        done = []
        for st in self._stations.list_by_mode("forward"):
            local_hour = ((now_s + st.utc_offset_min * 60) // HOUR_S) % 24
            if force or local_hour == self._daily_hour:
                try:
                    self._run.run(st, now_s)
                    done.append(st.dev_eui)
                except InsufficientData:
                    continue   # skip stations without a usable window this run
        return done


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

    def update_station(self, dev_eui: str, patch: dict) -> Station:
        st = self.station(dev_eui)
        for key in ("name", "mode"):
            if key in patch:
                setattr(st, key, str(patch[key]))
        for key in ("lat", "lon"):
            if key in patch:
                setattr(st, key, float(patch[key]))
        if "utc_offset_min" in patch:
            st.utc_offset_min = int(patch["utc_offset_min"])
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
