"""Dataset TA provider: the replay mapping shared with the firmware."""
import json

from app.adapters.dataset.forecast import (
    DATA_PATH,
    REPLAY_UTC_OFFSET_MIN,
    DatasetForecast,
    replay_row_now,
)
from config import Settings

HOUR = 3600
DAY0 = 1_789_000_000 // 86400 * 86400      # a UTC midnight


def test_row_now_follows_the_local_hour():
    assert replay_row_now(DAY0 + 10 * HOUR, 0) == 58          # h=10 -> base row
    assert replay_row_now(DAY0 + 9 * HOUR, 0) == 81           # h=9  -> last row
    assert replay_row_now(DAY0 + 11 * HOUR + 3599, 0) == 59   # whole hour, any minute
    assert replay_row_now(DAY0 + 8 * HOUR, 120) == 58         # 08:00 UTC = 10:00 local
    assert replay_row_now(DAY0 + 1 * HOUR, -300) == 68        # 01:00 UTC = 20:00 local
    assert replay_row_now(DAY0 + 9 * HOUR + 1800, 30) == 58   # half-hour offsets too


def test_every_window_stays_inside_the_table():
    rows = json.loads(DATA_PATH.read_text())["rows"]
    seen = set()
    for offset in (-720, -300, 0, 90, 120, 840):
        for h in range(24):
            row = replay_row_now(DAY0 + h * HOUR, offset)
            seen.add(row)
            assert 0 <= row - 47 and row + 24 <= rows - 1
    assert seen == set(range(58, 82))


def test_fetch_returns_the_dataset_window():
    ta = json.loads(DATA_PATH.read_text())["ta"]
    now = DAY0 + 10 * HOUR + 120
    fc = DatasetForecast(0, clock=lambda: now).fetch(39.47, -0.38)
    assert fc.past_ta == ta[11:59] and fc.future_ta == ta[59:83]   # rows 11..58 / 59..82
    assert len(fc.past_ta) == 48 and len(fc.future_ta) == 24
    assert fc.generated_at_ms == now * 1000


def test_fetch_anchors_on_the_fixed_replay_offset():
    """One constant shared with the firmware (default +120), wherever the station is."""
    ta = json.loads(DATA_PATH.read_text())["ta"]
    now = DAY0 + 10 * HOUR + 120                       # 12:02 under +120 -> row 60
    for lat, lon in ((39.47, -0.38), (4.6, -74.1)):
        fc = DatasetForecast(clock=lambda: now).fetch(lat, lon)
        assert fc.past_ta[-1] == ta[60] and fc.future_ta[0] == ta[61]
    assert Settings().replay_utc_offset_min == REPLAY_UTC_OFFSET_MIN == 120
