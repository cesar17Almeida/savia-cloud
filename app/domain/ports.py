"""Ports: the interfaces the application depends on. Adapters implement these."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import DownlinkCommand, Forecast, StationLink


class TtnPort(ABC):
    """Outbound port to The Things Network (schedule downlinks)."""

    @abstractmethod
    def schedule_downlink(self, command: DownlinkCommand) -> None: ...


class ForecastPort(ABC):
    """Outbound port to a weather forecast provider."""

    @abstractmethod
    def fetch(self, lat: float, lon: float) -> Forecast: ...


class StationRepository(ABC):
    """Persistence of per-station state."""

    @abstractmethod
    def get(self, dev_id: str) -> StationLink | None: ...

    @abstractmethod
    def save(self, link: StationLink) -> None: ...
