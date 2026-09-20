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


@dataclass(frozen=True)
class UplinkRecord:
    """One received uplink as logged: raw payload + decoded type + link quality."""
    dev_eui: str
    ts_s: int
    u_type: str
    payload_hex: str
    rssi: int | None
    snr: float | None
    id: int | None = None      # log row id (None until stored)


# downlink_log.status lifecycle. A config only reaches the node in its next RX
# window, so "queued at TTN" and "applied by the node" are different facts and the
# panel must never conflate them. Kept as the literal already in the DB.
DL_QUEUED = "scheduled"    # accepted by the TTN queue, not yet heard by the node
DL_DELIVERED = "delivered"  # handed to the station in its RX window (HTTP link only)
DL_APPLIED = "applied"     # the node answered with CFG_ACK
DL_FAILED = "failed"       # the push to TTN itself failed; nothing left the cloud
# The operator read the notice and put it away. Terminal like the two above, and it
# keeps the detail it had, so the log still says what happened -- only the banner goes.
DL_DISMISSED = "dismissed"
# The operator pulled it out of the queue before the station ever heard it. Terminal;
# the queue entry is gone but this row stays, so the history still says it existed.
DL_CANCELLED = "cancelled"


@dataclass(frozen=True)
class DownlinkRecord:
    """One scheduled downlink as logged."""
    dev_eui: str
    ts_s: int
    kind: str
    payload_hex: str
    status: str
    id: int | None = None      # log row id (None until stored)

    @property
    def state(self) -> str:
        """First token of status: DL_QUEUED / DL_DELIVERED / DL_APPLIED / DL_FAILED."""
        return self.status.split(":", 1)[0].strip()


@dataclass(frozen=True)
class QueuedDownlink:
    """One frame still waiting in the HTTP-link outbox for its station's RX window."""
    id: int
    dev_id: str
    f_port: int
    payload_hex: str
    created_s: int


@dataclass(frozen=True)
class ForecastRun:
    """One stored inference run: 24 hourly HS30 values (VWC 0..1)."""
    dev_eui: str
    run_ts_s: int
    hs30: list[float] = field(default_factory=list)
