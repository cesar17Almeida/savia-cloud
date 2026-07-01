"""Domain entities -- plain data, no framework or I/O."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalReading:
    """Link quality for a device. The UPLINK rssi/snr come from the TTN gateway
    metadata (node->gateway) -- the signal the station itself cannot measure."""
    rssi_dbm: int | None
    snr_db: float | None
    at_ms: int


@dataclass(frozen=True)
class Uplink:
    """A decoded uplink from a station: the HS30 forecast min + reception quality."""
    dev_id: str
    f_port: int
    hs30_min: float | None
    signal: SignalReading
    received_at_ms: int
    raw: bytes


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


@dataclass
class StationLink:
    """What the backend remembers per station."""
    dev_id: str
    lat: float
    lon: float
    last_uplink: Uplink | None = None
    last_signal: SignalReading | None = None
