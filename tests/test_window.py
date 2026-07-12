"""Pure LSTM-window assembly: LOCF fill, TA back-fill from the forecast, and the
insufficient-data guard. No interpreter / network needed."""
import pytest

from app.application.errors import InsufficientData
from app.application.services import HOUR_S, PAST_STEPS, build_lstm_window
from app.domain.models import Forecast, SoilReading

NOW = 1782000000                       # an exact hour boundary
LATEST = NOW - (NOW % HOUR_S)
FORECAST = Forecast(past_ta=[10.0] * PAST_STEPS, future_ta=[5.0] * 24, generated_at_ms=0)


def _hour(i: int) -> int:
    return LATEST - (PAST_STEPS - 1 - i) * HOUR_S


def _readings(skip=(), ta_none=()):
    """One reading per hour except `skip`; `ta_none` present hours carry no TA."""
    rows = []
    for i in range(PAST_STEPS):
        if i in skip:
            continue
        rows.append(SoilReading(
            dev_eui="EUI", ts_hour_s=_hour(i),
            hs10=0.80, hs30=0.70,
            ta=None if i in ta_none else 25.0,
        ))
    return rows


def test_full_window_shapes_and_ta_preference():
    ta, hs10, hs30, future = build_lstm_window(_readings(), FORECAST, NOW)
    assert len(ta) == 48 and len(hs10) == 48 and len(hs30) == 48 and len(future) == 24
    assert ta[10] == 25.0                        # station's own TA preferred
    assert future == [5.0] * 24


def test_interior_gap_locf_and_ta_backfill():
    ta, hs10, hs30, _ = build_lstm_window(_readings(skip=(20, 21, 22)), FORECAST, NOW)
    # soil holes carried forward from hour 19
    assert hs30[20] == hs30[19] == 0.70
    # TA holes (no reading) filled from the Open-Meteo past
    assert ta[20] == 10.0 and ta[19] == 25.0


def test_present_reading_without_ta_uses_forecast():
    ta, _, _, _ = build_lstm_window(_readings(ta_none=(30,)), FORECAST, NOW)
    assert ta[30] == 10.0                         # reading present but TA missing


def test_gap_over_six_hours_raises():
    with pytest.raises(InsufficientData):
        build_lstm_window(_readings(skip=(10, 11, 12, 13, 14, 15, 16)), FORECAST, NOW)


def test_no_soil_at_all_raises():
    with pytest.raises(InsufficientData):
        build_lstm_window([], FORECAST, NOW)
