"""Cloud FORWARD inference. Runs only when a tflite interpreter is installed;
otherwise skips cleanly (the plain .venv has no TensorFlow)."""
import pytest

from app.adapters.inference.lstm import LstmInference
from app.application.services import RunCloudInferenceService
from app.domain.models import Forecast, SoilReading, Station
from app.domain.ports import (
    DownlinkLogRepository,
    ForecastPort,
    ForecastRepository,
    ReadingRepository,
    TtnPort,
)
from config import Settings

MODEL_PATH = Settings().model_path
_infer = LstmInference(MODEL_PATH)
pytestmark = pytest.mark.skipif(
    not _infer.is_available(), reason="no tflite interpreter / model available"
)

HOUR = 3600
NOW = 1782000000


class _Readings(ReadingRepository):
    def upsert_soil(self, reading):  # unused here
        pass

    def window(self, dev_eui, from_ts, to_ts):
        return [
            SoilReading("EUI", from_ts + i * HOUR, hs10=0.79, hs30=0.77, ta=24.0)
            for i in range(48)
        ]

    def recent(self, dev_eui, limit):  # unused here
        return []


class _Forecasts(ForecastRepository):
    def __init__(self):
        self.runs = []

    def add_run(self, dev_eui, run_ts_s, hs30):
        self.runs.append((dev_eui, run_ts_s, list(hs30)))

    def latest_run(self, dev_eui):  # unused here
        return None


class _Weather(ForecastPort):
    def fetch(self, lat, lon):
        return Forecast(past_ta=[22.0] * 48, future_ta=[20.0] * 24, generated_at_ms=0)


class _Ttn(TtnPort):
    def __init__(self):
        self.pushed = []

    def schedule_downlink(self, command):
        self.pushed.append(command)


class _Log(DownlinkLogRepository):
    def __init__(self):
        self.entries = []

    def add(self, dev_eui, ts_s, kind, payload_hex, status):
        self.entries.append((dev_eui, kind, status))

    def list_recent(self, dev_eui, limit):  # unused here
        return []


def test_forward_inference_runs_and_schedules_downlink():
    forecasts, ttn, log = _Forecasts(), _Ttn(), _Log()
    svc = RunCloudInferenceService(_Readings(), forecasts, _Weather(), _infer, ttn, log)

    pred = svc.run(Station(dev_eui="EUI", lat=39.47, lon=-0.38), NOW)

    assert len(pred) == 24
    assert all(0.0 <= v <= 1.0 for v in pred)          # plausible VWC
    assert forecasts.runs and len(forecasts.runs[0][2]) == 24
    assert ttn.pushed and ttn.pushed[0].f_port == 8    # TIME_TA downlink queued
    assert log.entries and log.entries[0][1] == "time_ta"
