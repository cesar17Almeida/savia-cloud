"""Binary codec for the LoRaWAN payloads. MUST stay byte-compatible with the
firmware header savia_c/include/savia/lora_codec.h.

Uplink (node -> backend), 3 bytes:
  [0]     version (0x01)
  [1..2]  HS30 forecast min x1000, u16 big-endian (0xFFFF = unknown)

Downlink (backend -> node): header + variable TA arrays
  [0]     version (0x01)
  [1..4]  clock, epoch seconds, u32 big-endian (0 = no clock)
  [5]     n_past   (past TA hourly count, <= 48)
  [6]     n_future (future TA hourly count, <= 24)
  n_past   x TA degC as int8 (rounded)
  n_future x TA degC as int8 (rounded)
"""
from __future__ import annotations

VERSION = 0x01
TA_PAST_MAX = 48
TA_FUTURE_MAX = 24
UPLINK_LEN = 3
HS30_UNKNOWN = 0xFFFF


def decode_uplink(data: bytes) -> float | None:
    """Return the HS30 min (VWC 0..1), or None if the sentinel/unknown was sent."""
    if len(data) < UPLINK_LEN or data[0] != VERSION:
        raise ValueError("bad uplink frame")
    raw = (data[1] << 8) | data[2]
    return None if raw == HS30_UNKNOWN else raw / 1000.0


def encode_downlink(
    clock_epoch_s: int | None,
    past_ta: list[float],
    future_ta: list[float],
) -> bytes:
    """Encode the clock + TA window into the node's downlink frame."""
    if len(past_ta) > TA_PAST_MAX or len(future_ta) > TA_FUTURE_MAX:
        raise ValueError("TA array exceeds the max window")
    clock = int(clock_epoch_s or 0) & 0xFFFFFFFF
    out = bytearray()
    out.append(VERSION)
    out += clock.to_bytes(4, "big")
    out.append(len(past_ta))
    out.append(len(future_ta))
    out += bytes(_to_int8(t) for t in past_ta)
    out += bytes(_to_int8(t) for t in future_ta)
    return bytes(out)


def _to_int8(x: float) -> int:
    """Round to nearest and clamp to [-128, 127], as an unsigned byte (two's complement)."""
    v = max(-128, min(127, round(x)))
    return v & 0xFF
