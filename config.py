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
    # Default station location for the forecast (overridable per station).
    default_lat: float = 39.47
    default_lon: float = -0.38
    # Persistence. sqlite:///savia.db by default; tests pass sqlite:// (:memory:).
    db_url: str = "sqlite:///savia.db"
    # Shared secrets for the machine-to-machine endpoints (empty = auth disabled).
    webhook_secret: str = ""   # TTN webhook -> POST /ttn/uplink (X-Webhook-Token)
    cron_secret: str = ""      # external scheduler -> POST /cron/daily (X-Cron-Token)
    # LOCAL hour (0..23) at which each FORWARD station runs its daily inference; the
    # cron fires hourly and gates per station via its utc_offset_min.
    daily_hour: int = 6
    # On-device LSTM (mirror of the firmware model) for cloud FORWARD inference.
    model_path: str = (
        "/Users/calmeida/Documents/UPV_2025/TFM/docs/sensor_documentation/"
        "model/modelo_lstm/lstm_hs30_int8_pt.tflite"
    )
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
            db_url=os.getenv("DATABASE_URL", cls.db_url),
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            cron_secret=os.getenv("CRON_SECRET", ""),
            daily_hour=int(os.getenv("CRON_DAILY_HOUR", cls.daily_hour)),
            model_path=os.getenv("MODEL_PATH", cls.model_path),
            host=os.getenv("HOST", cls.host),
            port=int(os.getenv("PORT", cls.port)),
        )
