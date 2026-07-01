"""Open-Meteo adapter: fetch the hourly air-temperature (TA) window the LSTM needs."""
from __future__ import annotations

# import requests  # enable when wiring the real HTTP call

from config import Settings

from ...domain.models import Forecast
from ...domain.ports import ForecastPort


class OpenMeteoForecast(ForecastPort):
    def __init__(self, settings: Settings):
        self._s = settings

    def fetch(self, lat: float, lon: float) -> Forecast:
        # GET https://api.open-meteo.com/v1/forecast
        #   ?latitude={lat}&longitude={lon}&hourly=temperature_2m
        #   &past_days=2&forecast_days=2&timezone=UTC
        # then slice: last 48 hourly values (past) + next 24 (future) around "now".
        raise NotImplementedError("TODO: fetch + slice Open-Meteo temperature_2m -> Forecast")
