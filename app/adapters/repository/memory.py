"""In-memory station repository (kept for DB-free tests, same port)."""
from __future__ import annotations

from ...domain.models import Station
from ...domain.ports import StationRepository


class InMemoryStationRepository(StationRepository):
    def __init__(self) -> None:
        self._by_id: dict[str, Station] = {}

    def get(self, dev_eui: str) -> Station | None:
        return self._by_id.get(dev_eui)

    def save(self, station: Station) -> None:
        self._by_id[station.dev_eui] = station

    def list_by_mode(self, mode: str) -> list[Station]:
        return [s for s in self._by_id.values() if s.mode == mode]

    def list_all(self) -> list[Station]:
        return sorted(self._by_id.values(), key=lambda s: s.dev_eui)
