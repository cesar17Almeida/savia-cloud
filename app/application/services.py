"""Use cases: orchestrate domain + ports. No framework, no I/O details."""
from __future__ import annotations

from dataclasses import dataclass

from ..adapters.ttn import codec
from ..domain.models import DownlinkCommand, SignalReading, StationLink, Uplink
from ..domain.ports import ForecastPort, StationRepository, TtnPort

# FPort the station uses (mirror savia_c LORA_FPORT).
FPORT = 8


class IngestUplinkService:
    """Store the latest uplink + its (uplink) signal for a station."""

    def __init__(self, repo: StationRepository, default_lat: float, default_lon: float):
        self._repo = repo
        self._lat = default_lat
        self._lon = default_lon

    def handle(self, uplink: Uplink) -> None:
        link = self._repo.get(uplink.dev_id) or StationLink(uplink.dev_id, self._lat, self._lon)
        link.last_uplink = uplink
        link.last_signal = uplink.signal
        self._repo.save(link)


class ScheduleDownlinkService:
    """Build the clock + TA-forecast frame and push it to the station."""

    def __init__(self, repo: StationRepository, ttn: TtnPort, forecast: ForecastPort):
        self._repo = repo
        self._ttn = ttn
        self._forecast = forecast

    def run(self, dev_id: str, now_epoch_s: int) -> DownlinkCommand:
        link = self._repo.get(dev_id)
        lat = link.lat if link else 0.0
        lon = link.lon if link else 0.0
        fc = self._forecast.fetch(lat, lon)
        payload = codec.encode_downlink(now_epoch_s, fc.past_ta, fc.future_ta)
        cmd = DownlinkCommand(dev_id=dev_id, f_port=FPORT, payload=payload)
        self._ttn.schedule_downlink(cmd)
        return cmd


class SignalQueryService:
    """Read the last known signal for a station (uplink rssi/snr from TTN)."""

    def __init__(self, repo: StationRepository):
        self._repo = repo

    def last_signal(self, dev_id: str) -> SignalReading | None:
        link = self._repo.get(dev_id)
        return link.last_signal if link else None


@dataclass
class Services:
    """Simple container the HTTP layer reads from app.config["SERVICES"]."""

    ingest_uplink: IngestUplinkService
    schedule_downlink: ScheduleDownlinkService
    signal_query: SignalQueryService

    @classmethod
    def build(
        cls,
        repo: StationRepository,
        ttn: TtnPort,
        forecast: ForecastPort,
        *,
        default_lat: float = 0.0,
        default_lon: float = 0.0,
    ) -> "Services":
        return cls(
            ingest_uplink=IngestUplinkService(repo, default_lat, default_lon),
            schedule_downlink=ScheduleDownlinkService(repo, ttn, forecast),
            signal_query=SignalQueryService(repo),
        )
