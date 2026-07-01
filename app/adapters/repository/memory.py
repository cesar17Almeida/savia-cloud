"""In-memory station repository (swap for a DB adapter later, same port)."""
from __future__ import annotations

from ...domain.models import StationLink
from ...domain.ports import StationRepository


class InMemoryStationRepository(StationRepository):
    def __init__(self) -> None:
        self._by_id: dict[str, StationLink] = {}

    def get(self, dev_id: str) -> StationLink | None:
        return self._by_id.get(dev_id)

    def save(self, link: StationLink) -> None:
        self._by_id[link.dev_id] = link
