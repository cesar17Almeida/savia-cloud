"""Domain entities -- plain data, no framework or I/O."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class User:
    """A registered owner. pw_hash is a werkzeug hash -- never the plaintext."""
    email: str
    pw_hash: str
    id: int | None = None


@dataclass(frozen=True)
class Session:
    """A bearer token bound to a user, valid until expires_at (epoch seconds)."""
    token: str
    user_id: int
    expires_at: int


@dataclass
class Station:
    """A LoRaWAN station keyed by DevEUI. user_id is None until claimed."""
    dev_eui: str
    user_id: int | None = None
    name: str = ""
    lat: float = 0.0
    lon: float = 0.0
    utc_offset_min: int = 0
    mode: str = "forward"          # "forward" | "local"
    last_rssi: int | None = None
    last_snr: float | None = None
    last_uplink_at: int | None = None   # epoch seconds


@dataclass(frozen=True)
class SoilReading:
    """One hourly soil aggregate for a station. Any field may be None (gap)."""
    dev_eui: str
    ts_hour_s: int
    hs10: float | None
    hs30: float | None
    ta: float | None


@dataclass(frozen=True)
class Forecast:
    """Hourly air-temperature (TA) window the station's LSTM needs, degC."""
    past_ta: list[float]     # oldest -> newest, up to 48
    future_ta: list[float]   # next hours, up to 24
    generated_at_ms: int


@dataclass(frozen=True)
class DownlinkCommand:
    """An encoded frame to push to a device on a given FPort."""
    dev_id: str
    f_port: int
    payload: bytes
    confirmed: bool = False
