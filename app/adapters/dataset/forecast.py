"""Dataset adapter: serve the TA window from the LSTM training set (FORECAST_SOURCE=dataset).

replay_2018.json holds 120 hourly rows of node 4 (2018-08-30 00:00 local onwards),
so `row % 24` is the local hour of day. The row standing for "now" depends only on
the hour of day under ONE fixed UTC offset (REPLAY_UTC_OFFSET_MIN); the firmware
replays its soil series with the SAME mapping and the same constant, so the window
the station infers on matches the measured truth. The station's own offset is not
used: the app may change it mid-session and both ends would stop agreeing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from ...domain.models import Forecast
from ...domain.ports import ForecastPort

PAST_STEPS = 48
FUTURE_STEPS = 24
HOUR_S = 3600
BASE_ROW = 58      # row that stands for local hour BASE_HOUR
BASE_HOUR = 10
REPLAY_UTC_OFFSET_MIN = 120   # CEST; mirror of SAVIA_DEMO_UTC_OFFSET_MIN
DATA_PATH = Path(__file__).with_name("replay_2018.json")


def replay_row_now(now_s: int, utc_offset_min: int) -> int:
    """Dataset row for the current local hour: 58..81 (h=10 -> 58, h=9 -> 81)."""
    hour = ((now_s + utc_offset_min * 60) // HOUR_S) % 24
    return BASE_ROW + ((hour - BASE_HOUR) % 24)


class DatasetForecast(ForecastPort):
    def __init__(self, replay_offset_min: int = REPLAY_UTC_OFFSET_MIN,
                 path: Path | str = DATA_PATH,
                 clock: Callable[[], float] = time.time):
        data = json.loads(Path(path).read_text())
        self._ta = [float(t) for t in data["ta"]]
        self._offset_min = replay_offset_min
        self._clock = clock
        # Rows 58..81 can stand for "now": every window must fit inside the table.
        if BASE_ROW - PAST_STEPS + 1 < 0 or BASE_ROW + 23 + FUTURE_STEPS >= len(self._ta):
            raise ValueError("dataset does not cover the replay windows")

    def fetch(self, lat: float, lon: float) -> Forecast:
        """48 rows ending at the current replay hour + the 24 that follow."""
        now = self._clock()
        row = replay_row_now(int(now), self._offset_min)
        past = self._ta[row - PAST_STEPS + 1: row + 1]
        future = self._ta[row + 1: row + 1 + FUTURE_STEPS]
        return Forecast(past_ta=past, future_ta=future, generated_at_ms=int(now * 1000))
