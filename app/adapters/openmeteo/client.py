"""Open-Meteo adapter: fetch the hourly air-temperature (TA) window the LSTM needs.

Returns 48 past + 24 future hourly values aligned to the current UTC hour:
past[47] is the current hour, future[0] the next hour -- the exact alignment the
firmware's weather cache expects.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from config import Settings

from ...domain.models import Forecast
from ...domain.ports import ForecastPort

PAST_STEPS = 48
FUTURE_STEPS = 24


class OpenMeteoForecast(ForecastPort):
    def __init__(self, settings: Settings):
        self._s = settings

    def fetch(self, lat: float, lon: float) -> Forecast:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "past_days": 2,
            "forecast_days": 2,
            "timezone": "UTC",
        }
        resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
        if resp.status_code >= 400:
            raise RuntimeError(f"Open-Meteo fetch failed ({resp.status_code}): {resp.text[:300]}")
        hourly = (resp.json() or {}).get("hourly", {})
        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        if len(times) != len(temps) or not times:
            raise ValueError("Open-Meteo returned no hourly temperature series")

        # Epoch (s) per bucket; find the current UTC hour.
        epochs = [
            int(datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp())
            for t in times
        ]
        now = datetime.now(timezone.utc)
        latest_hour = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
        idx = max((i for i, e in enumerate(epochs) if e <= latest_hour), default=-1)
        if idx < PAST_STEPS - 1 or idx + FUTURE_STEPS >= len(temps):
            raise ValueError("Open-Meteo series does not cover the LSTM window")

        past = [float(temps[i]) for i in range(idx - PAST_STEPS + 1, idx + 1)]
        future = [float(temps[i]) for i in range(idx + 1, idx + 1 + FUTURE_STEPS)]
        return Forecast(past_ta=past, future_ta=future, generated_at_ms=int(now.timestamp() * 1000))
