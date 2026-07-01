"""Environment-based settings (12-factor). No secrets in code."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # TTN (The Things Stack, eu1 cluster).
    ttn_base_url: str = "https://eu1.cloud.thethings.network"
    ttn_app_id: str = "savia"
    ttn_api_key: str = ""
    # Default station location for the forecast (overridable per station later).
    default_lat: float = 39.47
    default_lon: float = -0.38
    # HTTP server.
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            ttn_base_url=os.getenv("TTN_BASE_URL", cls.ttn_base_url),
            ttn_app_id=os.getenv("TTN_APP_ID", cls.ttn_app_id),
            ttn_api_key=os.getenv("TTN_API_KEY", ""),
            default_lat=float(os.getenv("STATION_LAT", cls.default_lat)),
            default_lon=float(os.getenv("STATION_LON", cls.default_lon)),
            host=os.getenv("HOST", cls.host),
            port=int(os.getenv("PORT", cls.port)),
        )
