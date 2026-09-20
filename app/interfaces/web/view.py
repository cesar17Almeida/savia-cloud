"""View models for the operator panel.

Presentation-only reductions of what PanelService already returns: no new query
reaches past its methods, and nothing here decides anything the panel did not
already know. Kept out of routes.py so the request handlers stay readable.
"""
from __future__ import annotations

import time
from typing import Sequence

from ...adapters.ttn import codec
from ...domain.models import QueuedDownlink, SoilReading, Station
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


# --- downlinks: what a frame says, read back out of its bytes -----------------


def decode_downlink(kind: str, payload: bytes) -> dict:
    """A logged or queued downlink in operator language: a one-line summary, the TA
    window when it carries one, and the config fields when it is a patch. Undecodable
    bytes are reported as such rather than guessed at."""
    out: dict = {"summary": "no decodificable", "ta": None, "fields": None}
    try:
        if kind == "time_ta":
            d = codec.decode_downlink_time_ta(payload)
            clock = d["clock_epoch_s"]
            hour = (time.strftime("%H:%M:%S", time.gmtime(clock)) + " UTC"
                    if clock else "sin hora")
            past, future = d["past_ta"], d["future_ta"]
            if not past and not future:
                out["summary"] = f"sincronización de hora · {hour}"
                return out
            both = past + future
            out["summary"] = (f"hora {hour} + temperatura del aire: {len(past)} h "
                              f"anteriores y {len(future)} h de previsión")
            out["ta"] = {"past": past, "future": future,
                         "min": min(both), "max": max(both)}
        elif kind == "config":
            fields = codec.decode_config_patch_tlv(payload)
            out["fields"] = fields
            out["summary"] = ("configuración: "
                              + ", ".join(f"{k} = {v}" for k, v in fields.items()))
    except ValueError:
        pass
    return out


def downlink_kind(payload: bytes) -> str:
    """The frame's own type byte, for a queue entry that carries no label."""
    if len(payload) < 2 or payload[0] != codec.VERSION:
        return "unknown"
    return codec.DN_KINDS.get(payload[1], "unknown")


# --- the queue page -----------------------------------------------------------


def _queue_item(item: QueuedDownlink) -> dict:
    payload = bytes.fromhex(item.payload_hex)
    kind = downlink_kind(payload)
    return {"id": item.id, "kind": kind, "bytes": len(payload),
            "created_s": item.created_s, "f_port": item.f_port,
            "payload_hex": item.payload_hex, **decode_downlink(kind, payload)}


def queue_view(queue, now_s: int) -> dict:
    """The queue page: one group per station, frames in the order they will leave.

    A station that is held but has nothing waiting still gets a group -- otherwise
    the only control that could release it would have nowhere to live.
    """
    paused = queue.paused()
    stations = {s.dev_eui: s for s in queue.stations()}
    grouped: dict[str, list[dict]] = {}
    for item in queue.pending():
        grouped.setdefault(item.dev_id, []).append(_queue_item(item))

    groups = []
    for dev_id in sorted(set(grouped) | set(paused)):
        frames = grouped.get(dev_id, [])
        groups.append({
            "dev_eui": dev_id,
            "st": stations.get(dev_id),
            "paused_s": paused.get(dev_id),
            # Never "items": on a dict that name resolves to the method in Jinja.
            "frames": frames,
            "oldest_s": frames[0]["created_s"] if frames else None,
        })
    # The station that has been waiting longest leads; held ones sort with them.
    groups.sort(key=lambda g: (g["oldest_s"] is None, g["oldest_s"] or 0))

    waiting = [f for g in groups for f in g["frames"]]
    return {
        "groups": groups,
        "total": len(waiting),
        "stations_waiting": sum(1 for g in groups if g["frames"]),
        "paused_count": len(paused),
        "oldest_s": min((i["created_s"] for i in waiting), default=None),
    }


__all__ = ["ONLINE_WINDOW_S", "Sparkline", "decode_downlink", "downlink_kind",
           "fleet_card", "fleet_cards", "fleet_summary", "is_online",
           "latest_values", "newest", "queue_view"]
