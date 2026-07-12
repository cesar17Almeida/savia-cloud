"""Binary codec for the LoRaWAN payloads, wire v2. MUST stay byte-compatible with
the firmware (savia_c/include/savia/lora_codec.h + src/codec/lora_codec.c). Both
sides are pinned by the shared golden vectors in tests/test_codec.py /
savia_c/test/test_lora_codec.c -- change one side, change both.

All integers big-endian. Common header: [0]=version 0x02, [1]=message type.

Uplinks (node -> backend), decoded here:
  0x01 FORECAST  [2..3] hs30_min u16 x1000 (0xFFFF = unknown)
  0x02 SOIL      [2] n (1..4), then n x 10 B: ts_hour u32 | hs10 u16 x1000 |
                 hs30 u16 x1000 | ta i16 x10 (0xFFFF/0x7FFF = missing)
  0x03 COORDS    lat i32 x1e-7 | lon i32 x1e-7 | utc_offset_min i16
  0x04 CFG_ACK   applied u8 | rejected u8

Downlinks (backend -> node), encoded here:
  0x01 TIME_TA   clock u32 epoch s (0 = none) | n_past u8 | n_future u8 |
                 n_past x TA i8 | n_future x TA i8
  0x02 CONFIG    TLV sequence: [id u8][len u8][value big-endian]
"""
from __future__ import annotations

VERSION = 0x02

# Message types.
UP_FORECAST = 0x01
UP_SOIL = 0x02
UP_COORDS = 0x03
UP_CFG_ACK = 0x04
DN_TIME_TA = 0x01
DN_CONFIG = 0x02

TA_PAST_MAX = 48
TA_FUTURE_MAX = 24
SOIL_RECS_MAX = 4

HS_UNKNOWN = 0xFFFF   # u16 x1000 sentinel (forecast / soil moisture)
TA_UNKNOWN = 0x7FFF   # i16 x10 sentinel (air temperature)

# Config-patch TLV field ids (downlink 0x02) -- mirror LORA_CFG_* in the firmware.
CFG_SLEEP_S = 0x01
CFG_DEEP_SLEEP = 0x02
CFG_CAPTURE_S = 0x03
CFG_DAILY_HOUR = 0x04
CFG_LORA_PERIOD_S = 0x05
CFG_INFERENCE_MODE = 0x06
CFG_UTC_OFFSET_MIN = 0x07
CFG_IRRIGATION_HOUR = 0x08
CFG_LAT = 0x09
CFG_LON = 0x0A
CFG_LOG_LEVEL = 0x0B


# --- decode helpers ----------------------------------------------------------

def _u16(p: bytes) -> int:
    return (p[0] << 8) | p[1]


def _i16(p: bytes) -> int:
    v = _u16(p)
    return v - 0x10000 if v & 0x8000 else v


def _u32(p: bytes) -> int:
    return (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]


def _i32(p: bytes) -> int:
    v = _u32(p)
    return v - 0x100000000 if v & 0x80000000 else v


def _vwc(raw: int) -> float | None:
    return None if raw == HS_UNKNOWN else raw / 1000.0


# --- uplink decode (node -> backend) -----------------------------------------

def decode_uplink(data: bytes) -> dict:
    """Decode any uplink frame into a discriminated dict keyed by "type".

    Returns one of:
      {"type": "forecast", "hs30_min": float|None}
      {"type": "soil", "records": [{"ts_hour_s", "hs10", "hs30", "ta"}, ...]}
      {"type": "coords", "lat", "lon", "utc_offset_min"}
      {"type": "cfg_ack", "applied", "rejected"}
    Raises ValueError on a malformed / unknown frame.
    """
    if len(data) < 2 or data[0] != VERSION:
        raise ValueError("bad uplink frame")
    mtype = data[1]

    if mtype == UP_FORECAST:
        if len(data) < 4:
            raise ValueError("short forecast uplink")
        return {"type": "forecast", "hs30_min": _vwc(_u16(data[2:4]))}

    if mtype == UP_SOIL:
        if len(data) < 3:
            raise ValueError("short soil uplink")
        n = data[2]
        if not 1 <= n <= SOIL_RECS_MAX or len(data) < 3 + n * 10:
            raise ValueError("bad soil record count")
        records = []
        off = 3
        for _ in range(n):
            ta_raw = _i16(data[off + 8:off + 10])
            records.append({
                "ts_hour_s": _u32(data[off:off + 4]),
                "hs10": _vwc(_u16(data[off + 4:off + 6])),
                "hs30": _vwc(_u16(data[off + 6:off + 8])),
                "ta": None if ta_raw == TA_UNKNOWN else ta_raw / 10.0,
            })
            off += 10
        return {"type": "soil", "records": records}

    if mtype == UP_COORDS:
        if len(data) < 12:
            raise ValueError("short coords uplink")
        return {
            "type": "coords",
            "lat": _i32(data[2:6]) / 1e7,
            "lon": _i32(data[6:10]) / 1e7,
            "utc_offset_min": _i16(data[10:12]),
        }

    if mtype == UP_CFG_ACK:
        if len(data) < 4:
            raise ValueError("short cfg_ack uplink")
        return {"type": "cfg_ack", "applied": data[2], "rejected": data[3]}

    raise ValueError(f"unknown uplink type 0x{mtype:02X}")


# --- downlink encode (backend -> node) ---------------------------------------

def _to_int8(x: float) -> int:
    """Round to nearest (half away from zero, as the firmware does) and clamp to
    [-128, 127], returned as an unsigned byte (two's complement)."""
    r = x + 0.5 if x >= 0 else x - 0.5
    v = max(-128, min(127, int(r)))
    return v & 0xFF


def encode_downlink_time_ta(
    past_ta: list[float],
    future_ta: list[float],
    clock_epoch_s: int = 0,
) -> bytes:
    """Encode a TIME_TA downlink: clock + past/future air-temperature window.
    Empty arrays + a clock -> an 8-byte pure clock sync."""
    if len(past_ta) > TA_PAST_MAX or len(future_ta) > TA_FUTURE_MAX:
        raise ValueError("TA array exceeds the LSTM window")
    clock = int(clock_epoch_s or 0) & 0xFFFFFFFF
    out = bytearray([VERSION, DN_TIME_TA])
    out += clock.to_bytes(4, "big")
    out.append(len(past_ta))
    out.append(len(future_ta))
    out += bytes(_to_int8(t) for t in past_ta)
    out += bytes(_to_int8(t) for t in future_ta)
    return bytes(out)


# TLV field spec: id -> (byte width, big-endian signedness, range validator).
def _rng(lo: int, hi: int):
    def check(v: int) -> bool:
        return lo <= v <= hi
    return check


_TLV_FIELDS = {
    "sleep_s":         (CFG_SLEEP_S, 4, False, _rng(10, 86400)),
    "deep_sleep":      (CFG_DEEP_SLEEP, 1, False, _rng(0, 1)),
    "capture_s":       (CFG_CAPTURE_S, 4, False, _rng(60, 86400)),
    "daily_hour":      (CFG_DAILY_HOUR, 1, False, _rng(0, 23)),
    "lora_period_s":   (CFG_LORA_PERIOD_S, 4, False, _rng(300, 86400)),
    "inference_mode":  (CFG_INFERENCE_MODE, 1, False, _rng(0, 1)),
    "utc_offset_min":  (CFG_UTC_OFFSET_MIN, 2, True, _rng(-720, 840)),
    "irrigation_hour": (CFG_IRRIGATION_HOUR, 1, False, _rng(0, 23)),
    "lat":             (CFG_LAT, 4, True, _rng(-900000000, 900000000)),
    "lon":             (CFG_LON, 4, True, _rng(-1800000000, 1800000000)),
    "log_level":       (CFG_LOG_LEVEL, 1, False, _rng(0, 2)),
}

# Deterministic emit order (ascending field id) so the frame is testable.
_TLV_ORDER = sorted(_TLV_FIELDS, key=lambda k: _TLV_FIELDS[k][0])


def encode_config_patch_tlv(fields: dict) -> bytes:
    """Encode a CONFIG downlink from a patch dict. Keys: sleep_s, deep_sleep,
    capture_s, daily_hour, lora_period_s, inference_mode, utc_offset_min,
    irrigation_hour, lat, lon, log_level. lat/lon are degrees (scaled x1e-7 on the
    wire). Ranges are validated BEFORE encoding; out-of-range raises ValueError."""
    unknown = set(fields) - set(_TLV_FIELDS)
    if unknown:
        raise ValueError(f"unknown config field(s): {sorted(unknown)}")

    out = bytearray([VERSION, DN_CONFIG])
    for key in _TLV_ORDER:
        if key not in fields:
            continue
        fid, width, signed, in_range = _TLV_FIELDS[key]
        raw = fields[key]
        value = round(raw * 1e7) if key in ("lat", "lon") else int(raw)
        if not in_range(value):
            raise ValueError(f"{key}={raw} out of range")
        out.append(fid)
        out.append(width)
        out += value.to_bytes(width, "big", signed=signed)
    return bytes(out)
