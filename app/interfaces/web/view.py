"""View models for the operator panel.

Presentation-only reductions of what PanelService already returns: no new query
reaches past its methods, and nothing here decides anything the panel did not
already know. Kept out of routes.py so the request handlers stay readable.
"""
from __future__ import annotations

from typing import Sequence

from ...domain.models import SoilReading, Station
from .charts import Sparkline, build_sparkline

# A station counts as "en línea" while its last uplink is inside this window.
ONLINE_WINDOW_S = 24 * 3600
# Readings behind one fleet-card sparkline. Enough to show a day and a half.
FLEET_SPARK_READINGS = 36


def is_online(st: Station, now_s: int) -> bool:
    return bool(st.last_uplink_at and 0 <= now_s - st.last_uplink_at <= ONLINE_WINDOW_S)


def newest(readings: Sequence[SoilReading], key: str) -> float | None:
    """Most recent non-empty value of one field (panel.readings is newest first)."""
    for r in readings:
        v = getattr(r, key)
        if v is not None:
            return float(v)
    return None


def latest_values(readings: Sequence[SoilReading]) -> dict[str, float | None]:
    """The three current measurements a station leads with."""
    return {k: newest(readings, k) for k in ("hs10", "hs30", "ta")}


def fleet_card(panel, st: Station, now_s: int) -> dict:
    """One station as the fleet page shows it: its row, its state and its trend."""
    readings = panel.readings(st.dev_eui, FLEET_SPARK_READINGS)
    return {
        "st": st,
        "online": is_online(st, now_s),
        "hs30": newest(readings, "hs30"),
        "spark": build_sparkline(readings),
    }


def fleet_cards(panel, now_s: int) -> list[dict]:
    return [fleet_card(panel, st, now_s) for st in panel.stations()]


def fleet_summary(cards: Sequence[dict]) -> dict:
    """Fleet-wide counters for the stat row; all of it read off the cards."""
    stamps = [c["st"].last_uplink_at for c in cards if c["st"].last_uplink_at]
    return {
        "total": len(cards),
        "online": sum(1 for c in cards if c["online"]),
        "local": sum(1 for c in cards if c["st"].mode == "local"),
        "last_uplink_at": max(stamps) if stamps else None,
    }


__all__ = ["ONLINE_WINDOW_S", "Sparkline", "fleet_card", "fleet_cards",
           "fleet_summary", "is_online", "latest_values", "newest"]
