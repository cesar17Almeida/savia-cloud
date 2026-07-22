"""Ports: the interfaces the application depends on. Adapters implement these."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
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


# --- driven ports: outbound integrations -------------------------------------

class TtnPort(ABC):
    """Outbound port to The Things Network (schedule downlinks)."""

    @abstractmethod
    def schedule_downlink(self, command: DownlinkCommand) -> None: ...


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
    def list_recent(self, dev_eui: str, limit: int) -> list[DownlinkRecord]:
        """Latest scheduled downlinks, newest first."""
        ...


class UplinkLogRepository(ABC):
    """Raw history of received uplinks (payload + decoded type + link quality)."""

    @abstractmethod
    def add(self, record: UplinkRecord) -> None: ...

    @abstractmethod
    def list_recent(self, dev_eui: str, limit: int) -> list[UplinkRecord]:
        """Latest received uplinks, newest first."""
        ...
