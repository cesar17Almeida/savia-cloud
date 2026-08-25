"""Pure LSTM-window assembly: LOCF fill, TA back-fill from the forecast, and the
insufficient-data guard. No interpreter / network needed."""
import pytest

from app.application.errors import InsufficientData
from app.application.services import (
    HOUR_S,
    MAX_NEWEST_AGE_H,
    MIN_REAL_HOURS,
    PAST_STEPS,
    build_lstm_window,
)
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


# --- admission guards (same three the firmware applies) ----------------------

# Every other hour real: exactly MIN_REAL_HOURS buckets, no gap longer than 1 h and
# the newest bucket real -- isolates the coverage guard from the other two.
_ALTERNATE = tuple(range(0, PAST_STEPS, 2))          # skip the even hours


def test_below_coverage_floor_raises():
    """Too few real hours: LOCF would manufacture the window out of a handful of
    samples, so gathering fails instead."""
    skip = _ALTERNATE + (1,)                          # one below the floor
    with pytest.raises(InsufficientData, match="real soil hours"):
        build_lstm_window(_readings(skip=skip), FORECAST, NOW)


def test_coverage_floor_exactly_met_passes():
    _, _, hs30, _ = build_lstm_window(_readings(skip=_ALTERNATE), FORECAST, NOW)
    assert len(hs30) == PAST_STEPS
    assert hs30[0] == 0.70            # leading gap back-filled from the first real one


def test_stale_newest_bucket_raises():
    """The newest buckets are copies: the forecast would start from soil that may no
    longer exist."""
    stale = tuple(range(PAST_STEPS - (MAX_NEWEST_AGE_H + 1), PAST_STEPS))
    with pytest.raises(InsufficientData, match="newest real soil reading"):
        build_lstm_window(_readings(skip=stale), FORECAST, NOW)


def test_newest_bucket_within_tolerance_passes():
    stale = tuple(range(PAST_STEPS - MAX_NEWEST_AGE_H, PAST_STEPS))
    _, _, hs30, _ = build_lstm_window(_readings(skip=stale), FORECAST, NOW)
    assert hs30[-1] == 0.70           # carried forward from the last real hour
