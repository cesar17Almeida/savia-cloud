"""Codec must stay byte-compatible with the firmware (savia_c lora_codec.h)."""
from app.adapters.ttn import codec


def test_decode_uplink_value():
    # 0.123 VWC -> 123 -> 0x007B
    assert codec.decode_uplink(bytes([0x01, 0x00, 0x7B])) == 123 / 1000.0


def test_decode_uplink_unknown_sentinel():
    assert codec.decode_uplink(bytes([0x01, 0xFF, 0xFF])) is None


def test_decode_uplink_rejects_bad_version():
    try:
        codec.decode_uplink(bytes([0x02, 0x00, 0x00]))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_encode_downlink_header_and_int8():
    # clock=1, past=[20.0, -5.4], future=[22.6]
    frame = codec.encode_downlink(1, [20.0, -5.4], [22.6])
    assert frame[0] == 0x01                    # version
    assert frame[1:5] == (1).to_bytes(4, "big")  # clock u32 BE
    assert frame[5] == 2                        # n_past
    assert frame[6] == 1                        # n_future
    assert frame[7] == 20                       # round(20.0)
    assert frame[8] == (-5 & 0xFF)              # round(-5.4) -> -5 two's complement
    assert frame[9] == 23                       # round(22.6)
    assert len(frame) == 7 + 2 + 1
