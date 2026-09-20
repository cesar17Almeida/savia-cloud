"""Soil-reading chart: pure geometry for the panel's inline SVG. No Flask, no JS.

Readings in, viewBox coordinates out -- the template only draws what it gets.
HS10 and HS30 share one panel (both are VWC 0..1); TA rides a second, smaller
panel below it, sharing the time axis. A second y-scale on the same plot would
invent a correlation between soil water and air temperature, so it is not used.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from ...domain.models import SoilReading

# --- viewBox layout (units, not pixels: the SVG scales to the card) ----------
_W = 960.0
_PAD_L, _PAD_R = 54.0, 58.0          # y-tick labels left, end labels right
_SOIL_TITLE_Y, _SOIL_TOP, _SOIL_BOTTOM = 22.0, 32.0, 204.0
_TA_TITLE_Y, _TA_TOP, _TA_BOTTOM = 236.0, 246.0, 316.0
_XLAB_DY = 20.0                      # hour labels, below the lowest panel
_LEFT, _RIGHT = _PAD_L, _W - _PAD_R

# Rows are hourly aggregates: a longer hole is missing data, so the line is cut
# there instead of bridging it with a segment nobody measured.
_MAX_GAP_S = 3 * 3600
_MAX_X_TICKS = 6
# Widest hour label ("DD/MM HH:MM" at 13 units) plus air, so labels never touch.
_MIN_TICK_GAP = 92.0

# Candidate tick steps, finest first: the first one that lands few enough round
# values inside the domain wins.
_VWC_STEPS = (0.02, 0.05, 0.1, 0.2, 0.5)
_TA_STEPS = (1.0, 2.0, 5.0, 10.0, 20.0)


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Run:
    """One unbroken stretch of a series. A single point draws a dot, not a line."""
    points: list[Point]

    @property
    def attr(self) -> str:
        """The SVG polyline 'points' attribute."""
        return " ".join(f"{p.x},{p.y}" for p in self.points)


@dataclass(frozen=True)
class Tick:
    pos: float
    label: str


@dataclass(frozen=True)
class EndLabel:
    x: float
    y: float
    text: str


@dataclass(frozen=True)
class Series:
    key: str
    label: str
    color: str          # CSS custom property holding the series hue
    runs: list[Run]
    dots: list[Point]   # isolated points and the last one: markers worth drawing
    end: EndLabel | None


@dataclass(frozen=True)
class Panel:
    key: str
    title: str
    title_y: float
    top: float
    bottom: float
    y_ticks: list[Tick]
    series: list[Series]


@dataclass(frozen=True)
class Band:
    """Full-height hover target for one hour: the native SVG <title> readout."""
    x: float
    width: float
    text: str


@dataclass(frozen=True)
class SoilChart:
    width: float
    height: float
    left: float
    right: float
    soil: Panel
    ta: Panel | None
    x_ticks: list[Tick]
    x_label_y: float
    band_top: float
    band_bottom: float
    bands: list[Band]
    range_label: str
    aria_label: str

    @property
    def panels(self) -> list[Panel]:
        return [p for p in (self.soil, self.ta) if p is not None]


def _fmt_local(ts_s: int, offset_min: int, pattern: str) -> str:
    """Epoch seconds rendered in the station's local time (same shift as the dt filter)."""
    return time.strftime(pattern, time.gmtime(int(ts_s) + offset_min * 60))


def _domain(values: list[float], min_span: float,
            lo_limit: float | None = None, hi_limit: float | None = None
            ) -> tuple[float, float]:
    """Domain hugging the values, widened so flat data is not amplified into noise."""
    lo, hi = min(values), max(values)
    if hi - lo < min_span:
        mid = (lo + hi) / 2.0
        lo, hi = mid - min_span / 2.0, mid + min_span / 2.0
    else:
        pad = (hi - lo) * 0.12
        lo, hi = lo - pad, hi + pad
    if lo_limit is not None:
        lo = max(lo, lo_limit)
    if hi_limit is not None:
        hi = min(hi, hi_limit)
    if hi <= lo:                      # clamped to nothing: keep a readable sliver
        hi = lo + min_span
    return lo, hi


def _y_ticks(lo: float, hi: float, steps: Sequence[float], max_ticks: int,
             bottom: float, top: float, fmt: Callable[[float], str]) -> list[Tick]:
    """Round values that fall inside the domain: the domain hugs the data and the
    labels stay round, instead of padding the domain out to the next round number."""
    best: list[float] = []
    for step in steps:
        lowest = math.ceil(lo / step - 1e-9)
        highest = math.floor(hi / step + 1e-9)
        vals = [i * step for i in range(int(lowest), int(highest) + 1)]
        if not best:
            best = vals
        if 2 <= len(vals) <= max_ticks:
            best = vals
            break
    return [Tick(_round(_scale(v, lo, hi, bottom, top)), fmt(v)) for v in best]


def _scale(v: float, lo: float, hi: float, at_lo: float, at_hi: float) -> float:
    if hi == lo:
        return (at_lo + at_hi) / 2.0
    return at_lo + (v - lo) / (hi - lo) * (at_hi - at_lo)


def _round(v: float) -> float:
    return round(v, 1)


def _build_series(rows: list[SoilReading], key: str, label: str, color: str,
                  x_of: Callable[[int], float],
                  y_of: Callable[[float], float]) -> Series | None:
    """Split one field into unbroken runs; None values and long holes cut the line."""
    runs: list[list[Point]] = []
    cur: list[Point] = []
    last_ts: int | None = None
    for r in rows:
        v = getattr(r, key)
        if v is None:
            if cur:
                runs.append(cur)
                cur = []
            continue
        if last_ts is not None and r.ts_hour_s - last_ts > _MAX_GAP_S and cur:
            runs.append(cur)
            cur = []
        cur.append(Point(_round(x_of(r.ts_hour_s)), _round(y_of(float(v)))))
        last_ts = r.ts_hour_s
    if cur:
        runs.append(cur)
    if not runs:
        return None
    # Markers: every lone point, plus the newest one so the direct label has an anchor.
    dots = [run[0] for run in runs if len(run) == 1]
    last = runs[-1][-1]
    if last not in dots:
        dots.append(last)
    return Series(key, label, color, [Run(r) for r in runs], dots, None)


def _with_end_labels(series: list[Series], fmt: Callable[[float], str],
                     rows: list[SoilReading], top: float, bottom: float
                     ) -> list[Series]:
    """Label the newest value of each series. When two share a panel the upper one
    rides above its point and the lower below, so a label never leaves its line."""
    order = sorted(range(len(series)), key=lambda i: series[i].runs[-1].points[-1].y)
    labelled = list(series)
    for rank, i in enumerate(order):
        s = series[i]
        value = _last_value(rows, s.key)
        if value is None:                      # unreachable: a series exists only
            continue                           # because it has at least one value
        last = s.runs[-1].points[-1]
        dy = -10.0 if rank == 0 else 20.0
        y = min(max(last.y + dy, top + 12.0), bottom - 3.0)
        labelled[i] = Series(s.key, s.label, s.color, s.runs, s.dots,
                             EndLabel(_round(last.x + 8.0), _round(y), fmt(value)))
    return labelled


def _last_value(rows: list[SoilReading], key: str) -> float | None:
    for r in reversed(rows):
        v = getattr(r, key)
        if v is not None:
            return float(v)
    return None


def _vwc(v: float) -> str:
    return f"{v:.2f}"


def _vwc3(v: float) -> str:
    return f"{v:.3f}"


def _deg(v: float) -> str:
    return f"{v:.0f}"


def _deg1(v: float) -> str:
    return f"{v:.1f}"


def build_soil_chart(readings: Sequence[SoilReading],
                     utc_offset_min: int = 0) -> SoilChart | None:
    """Geometry for the soil-reading chart, or None when there is nothing to plot."""
    rows = sorted((r for r in readings if r.ts_hour_s), key=lambda r: r.ts_hour_s)
    if not rows:
        return None
    soil_vals = [float(v) for r in rows for v in (r.hs10, r.hs30) if v is not None]
    ta_vals = [float(r.ta) for r in rows if r.ta is not None]
    if not soil_vals and not ta_vals:
        return None

    t0, t1 = rows[0].ts_hour_s, rows[-1].ts_hour_s

    def x_of(ts: int) -> float:
        return _scale(ts, t0, t1, _LEFT, _RIGHT)

    # --- soil panel (VWC 0..1, both depths on one scale) ---------------------
    lo, hi = _domain(soil_vals or [0.0, 1.0], 0.10, 0.0, 1.0)

    def y_soil(v: float) -> float:
        return _scale(v, lo, hi, _SOIL_BOTTOM, _SOIL_TOP)

    soil_series = [s for s in (
        _build_series(rows, "hs10", "HS10 · 10 cm", "--viz-hs10", x_of, y_soil),
        _build_series(rows, "hs30", "HS30 · 30 cm", "--viz-hs30", x_of, y_soil),
    ) if s is not None]
    soil_series = _with_end_labels(soil_series, _vwc3, rows, _SOIL_TOP, _SOIL_BOTTOM)
    soil = Panel("soil", "Humedad volumétrica del suelo (VWC)", _SOIL_TITLE_Y,
                 _SOIL_TOP, _SOIL_BOTTOM,
                 _y_ticks(lo, hi, _VWC_STEPS, 5, _SOIL_BOTTOM, _SOIL_TOP, _vwc),
                 soil_series)

    # --- TA panel: its own scale, its own box, same time axis ----------------
    ta: Panel | None = None
    if ta_vals:
        tlo, thi = _domain(ta_vals, 4.0)

        def y_ta(v: float) -> float:
            return _scale(v, tlo, thi, _TA_BOTTOM, _TA_TOP)

        ta_series = _build_series(rows, "ta", "TA", "--viz-ta", x_of, y_ta)
        if ta_series is not None:
            ta = Panel("ta", "Temperatura del aire, TA (°C)", _TA_TITLE_Y,
                       _TA_TOP, _TA_BOTTOM,
                       _y_ticks(tlo, thi, _TA_STEPS, 4, _TA_BOTTOM, _TA_TOP, _deg),
                       _with_end_labels([ta_series], _deg1, rows, _TA_TOP, _TA_BOTTOM))

    # The box grows with the panels so the hour labels are never cut off.
    bottom_panel = ta or soil
    x_ticks = _x_ticks(rows, x_of, utc_offset_min)

    return SoilChart(
        width=_W,
        height=bottom_panel.bottom + _XLAB_DY + 14.0,
        left=_LEFT,
        right=_RIGHT,
        soil=soil,
        ta=ta,
        x_ticks=x_ticks,
        x_label_y=_round(bottom_panel.bottom + _XLAB_DY),
        band_top=_SOIL_TOP,
        band_bottom=bottom_panel.bottom,
        bands=_bands(rows, x_of, utc_offset_min),
        range_label=(f"{_fmt_local(t0, utc_offset_min, '%d/%m %H:%M')} → "
                     f"{_fmt_local(t1, utc_offset_min, '%d/%m %H:%M')}"),
        aria_label=_aria(rows, lo, hi, ta is not None),
    )


def _tick_indexes(n: int) -> list[int]:
    """Up to _MAX_X_TICKS reading positions, evenly spread, first and last included."""
    if n <= _MAX_X_TICKS:
        return list(range(n))
    step = (n - 1) / (_MAX_X_TICKS - 1)
    return sorted({int(round(i * step)) for i in range(_MAX_X_TICKS)})


def _x_ticks(rows: list[SoilReading], x_of: Callable[[int], float],
             offset_min: int) -> list[Tick]:
    """Hour labels in station local time. Candidates are spread over the readings,
    then thinned by distance -- readings bunched in time would otherwise print two
    labels on top of each other. The first tick of a new local day carries its date,
    so a window spanning midnight never shows two bare, equal hours."""
    stamps = [rows[i].ts_hour_s for i in _tick_indexes(len(rows))]
    kept: list[int] = []
    for ts in stamps:
        if not kept or x_of(ts) - x_of(kept[-1]) >= _MIN_TICK_GAP:
            kept.append(ts)
    if kept and kept[-1] != stamps[-1]:          # the right edge always gets a label
        while len(kept) > 1 and x_of(stamps[-1]) - x_of(kept[-1]) < _MIN_TICK_GAP:
            kept.pop()
        kept.append(stamps[-1])

    ticks, prev_day = [], ""
    for ts in kept:
        label = _fmt_local(ts, offset_min, "%H:%M")
        day = _fmt_local(ts, offset_min, "%d/%m")
        if day != prev_day:
            label, prev_day = f"{day} {label}", day
        ticks.append(Tick(_round(x_of(ts)), label))
    return ticks


def _bands(rows: list[SoilReading], x_of: Callable[[int], float],
           offset_min: int) -> list[Band]:
    """One hover band per hour, listing every series at that time."""
    xs = [x_of(r.ts_hour_s) for r in rows]
    out = []
    for i, r in enumerate(rows):
        left = (xs[i] + xs[i - 1]) / 2.0 if i else _LEFT
        right = (xs[i] + xs[i + 1]) / 2.0 if i + 1 < len(xs) else _RIGHT
        parts = [_fmt_local(r.ts_hour_s, offset_min, "%d/%m %H:%M")]
        parts.append(f"HS10 {_vwc3(r.hs10)}" if r.hs10 is not None else "HS10 —")
        parts.append(f"HS30 {_vwc3(r.hs30)}" if r.hs30 is not None else "HS30 —")
        parts.append(f"TA {_deg1(r.ta)} °C" if r.ta is not None else "TA —")
        out.append(Band(_round(left), _round(max(right - left, 1.0)),
                        " · ".join(parts)))
    return out


def _aria(rows: list[SoilReading], lo: float, hi: float, has_ta: bool) -> str:
    """One sentence for screen readers; the table below carries the values."""
    txt = (f"Humedad del suelo a 10 y 30 cm en {len(rows)} hora(s), "
           f"de {_vwc(lo)} a {_vwc(hi)} VWC")
    return txt + (", con la temperatura del aire debajo." if has_ta else ".")


# --- sparkline: the trend inside a fleet card --------------------------------
# Drawn edge to edge and stretched by the card (preserveAspectRatio="none"), so
# the stroke is kept honest with vector-effect and no round marker is drawn -- an
# ellipse is what a circle becomes under a non-uniform scale. The current value
# rides beside the sparkline as text, which is also the relief HS30 needs for its
# sub-3:1 contrast.
_SPARK_W, _SPARK_H, _SPARK_PAD = 300.0, 42.0, 3.0


@dataclass(frozen=True)
class Sparkline:
    width: float
    height: float
    line: str        # polyline "points"
    area: str        # the same run closed onto the baseline
    color: str
    aria_label: str


def build_sparkline(readings: Sequence[SoilReading], key: str = "hs30",
                    color: str = "--viz-hs30") -> Sparkline | None:
    """Trend of one field over the readings given, or None with fewer than two."""
    rows = sorted((r for r in readings if r.ts_hour_s), key=lambda r: r.ts_hour_s)
    values = [(r.ts_hour_s, float(getattr(r, key)))
              for r in rows if getattr(r, key) is not None]
    if len(values) < 2:
        return None
    t0, t1 = values[0][0], values[-1][0]
    lo, hi = _domain([v for _, v in values], 0.02)
    pts = [(_round(_scale(ts, t0, t1, 0.0, _SPARK_W)),
            _round(_scale(v, lo, hi, _SPARK_H - _SPARK_PAD, _SPARK_PAD)))
           for ts, v in values]
    line = " ".join(f"{x},{y}" for x, y in pts)
    area = f"{pts[0][0]},{_SPARK_H} {line} {pts[-1][0]},{_SPARK_H}"
    return Sparkline(_SPARK_W, _SPARK_H, line, area, color,
                     f"Tendencia de {key.upper()} en {len(values)} lecturas, "
                     f"de {_vwc3(min(v for _, v in values))} a "
                     f"{_vwc3(max(v for _, v in values))} VWC")


# --- payloads for the client charts ------------------------------------------
# The interactive chart is an enhancement over the inline SVG above; it reads
# these plain values. Instants are shifted into station-local and read back as
# UTC, exactly the trick the `dt` template filter uses, so every hour printed in
# the panel -- axis, tooltip, table -- is the same hour.


def _opt(v) -> float | None:
    return None if v is None else float(v)


def readings_payload(readings: Sequence[SoilReading],
                     utc_offset_min: int = 0) -> dict:
    """Stored readings as {offset_min, rows: [{t(ms), hs10, hs30, ta}]}, oldest
    first. The offset travels with them so the client can turn a UTC instant --
    what the table rows carry -- into the same shifted scale the points use."""
    rows = sorted((r for r in readings if r.ts_hour_s), key=lambda r: r.ts_hour_s)
    return {"offset_min": utc_offset_min,
            "rows": [{"t": (r.ts_hour_s + utc_offset_min * 60) * 1000,
                      "hs10": _opt(r.hs10), "hs30": _opt(r.hs30), "ta": _opt(r.ta)}
                     for r in rows]}


def forecast_payload(run, utc_offset_min: int = 0) -> dict | None:
    """The stored run as {min, rows: [{t(ms), hs30}]}, one point per hour ahead."""
    if run is None or not run.hs30:
        return None
    base = run.run_ts_s + utc_offset_min * 60
    values = [float(v) for v in run.hs30]
    return {"min": min(values),
            "rows": [{"t": (base + (i + 1) * 3600) * 1000, "hs30": v}
                     for i, v in enumerate(values)]}
