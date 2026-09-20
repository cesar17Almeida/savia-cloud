"""Ports: the interfaces the application depends on. Adapters implement these."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
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


# --- driven ports: outbound integrations -------------------------------------

class TtnPort(ABC):
    """Outbound port to The Things Network (downlinks + device provisioning)."""

    @abstractmethod
    def schedule_downlink(self, command: DownlinkCommand) -> None: ...

    @abstractmethod
    def register_device(self, device_id: str, dev_eui: str, join_eui: str,
                        app_key: str) -> None:
        """Provision an OTAA end device in the LNS registries."""
        ...


class ForecastPort(ABC):
    """Outbound port to a weather forecast provider."""

    @abstractmethod
    def fetch(self, lat: float, lon: float) -> Forecast: ...


class InferencePort(ABC):
    """Outbound port to the LSTM (cloud FORWARD inference)."""

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def predict_hs30(
        self,
        ta_past: list[float],
        hs10_past: list[float],
        hs30_past: list[float],
        ta_future: list[float],
    ) -> list[float]:
        """Return the 24 h HS30 forecast (VWC 0..1) from real-unit inputs."""
        ...


# --- driven ports: persistence -----------------------------------------------

class StationRepository(ABC):
    """Persistence of station state (keyed by DevEUI)."""

    @abstractmethod
    def get(self, dev_eui: str) -> Station | None: ...

    @abstractmethod
    def save(self, station: Station) -> None: ...

    @abstractmethod
    def list_by_mode(self, mode: str) -> list[Station]: ...

    @abstractmethod
    def list_all(self) -> list[Station]: ...


class UserRepository(ABC):
    @abstractmethod
    def get_by_email(self, email: str) -> User | None: ...

    @abstractmethod
    def get_by_id(self, user_id: int) -> User | None: ...

    @abstractmethod
    def add(self, email: str, pw_hash: str) -> User: ...

    @abstractmethod
    def update_password(self, user_id: int, pw_hash: str) -> None: ...


class SessionRepository(ABC):
    @abstractmethod
    def create(self, session: Session) -> None: ...

    @abstractmethod
    def get(self, token: str) -> Session | None: ...


class ReadingRepository(ABC):
    @abstractmethod
    def upsert_soil(self, reading: SoilReading) -> None: ...

    @abstractmethod
    def window(self, dev_eui: str, from_ts: int, to_ts: int) -> list[SoilReading]:
        """Soil readings with from_ts <= ts_hour_s <= to_ts, oldest first."""
        ...

    @abstractmethod
    def recent(self, dev_eui: str, limit: int) -> list[SoilReading]:
        """Latest soil readings, newest first."""
        ...


class ForecastRepository(ABC):
    @abstractmethod
    def add_run(self, dev_eui: str, run_ts_s: int, hs30: list[float]) -> None:
        """Store a 24 h forecast run (horizon_h 1..len)."""
        ...

    @abstractmethod
    def latest_run(self, dev_eui: str) -> ForecastRun | None:
        """Most recent stored run, hs30 ordered by horizon."""
        ...


class DownlinkLogRepository(ABC):
    @abstractmethod
    def add(self, dev_eui: str, ts_s: int, kind: str, payload_hex: str, status: str) -> None: ...

    @abstractmethod
    def list_recent(self, dev_eui: str, limit: int,
                    kind: str | None = None) -> list[DownlinkRecord]:
        """Latest scheduled downlinks, newest first, optionally of one kind."""
        ...

    @abstractmethod
    def confirm_oldest_queued(self, dev_eui: str, kind: str, status: str) -> bool:
        """Close the OLDEST downlink of that kind still awaiting its answer (queued or
        delivered): the queue drains FIFO, so a CFG_ACK answers the first config
        pushed, not the last. False when nothing was waiting."""
        ...

    @abstractmethod
    def set_status(self, row_id: int, status: str) -> bool:
        """Replace one logged downlink's status. False when the row is gone."""
        ...

    @abstractmethod
    def mark_delivered(self, dev_eui: str, payload_hex: str, status: str) -> bool:
        """Move the OLDEST queued downlink carrying that payload to `status`. False
        when no queued row matches."""
        ...


class LinkOutboxRepository(ABC):
    """Downlinks waiting for a station on the HTTP link (FIFO per device)."""

    @abstractmethod
    def add(self, dev_id: str, f_port: int, payload: bytes, now_s: int) -> None: ...

    @abstractmethod
    def take_next(self, dev_id: str, now_s: int) -> DownlinkCommand | None:
        """Oldest undelivered downlink of that device, marked delivered; None if
        empty OR while the device is paused -- a held queue hands out nothing."""
        ...

    @abstractmethod
    def pending(self, dev_id: str) -> int:
        """How many downlinks still wait for that device."""
        ...

    @abstractmethod
    def deliveries(self, dev_id: str, limit: int) -> dict[str, int]:
        """payload_hex -> delivered_s of the latest delivered downlinks."""
        ...

    @abstractmethod
    def list_pending(self, dev_id: str | None = None) -> list[QueuedDownlink]:
        """Everything still waiting, oldest first; the whole fleet when dev_id is None."""
        ...

    @abstractmethod
    def remove(self, item_id: int) -> QueuedDownlink | None:
        """Drop one undelivered entry from the queue and return what it was, so the
        caller can close its logged row. None when it is gone or already delivered."""
        ...

    @abstractmethod
    def set_paused(self, dev_id: str, paused: bool, now_s: int) -> None:
        """Hold or release that device's queue."""
        ...

    @abstractmethod
    def paused(self) -> dict[str, int]:
        """dev_id -> the instant its queue was held, for every held device."""
        ...


class UplinkLogRepository(ABC):
    """Raw history of received uplinks (payload + decoded type + link quality)."""

    @abstractmethod
    def add(self, record: UplinkRecord) -> None: ...

    @abstractmethod
    def list_recent(self, dev_eui: str, limit: int) -> list[UplinkRecord]:
        """Latest received uplinks, newest first."""
        ...
