"""Codec must stay byte-compatible with the firmware (savia_c lora_codec, wire v2).
The GOLDEN vectors below are the SAME literal bytes as savia_c/test/test_lora_codec.c
-- if one side changes, both change."""
import pytest

from app.adapters.ttn import codec


def hexb(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# --- uplink decode goldens ---------------------------------------------------

def test_forecast_golden():
    assert codec.decode_uplink(hexb("02 01 02 E0")) == {"type": "forecast", "hs30_min": 0.736}
    assert codec.decode_uplink(hexb("02 01 FF FF")) == {"type": "forecast", "hs30_min": None}


def test_soil_golden():
    frame = hexb("02 02 02 6A 37 29 80 03 35 02 E4 00 D7 "
                 "6A 37 37 90 FF FF 02 E5 FF E7")
    out = codec.decode_uplink(frame)
    assert out["type"] == "soil"
    r0, r1 = out["records"]
    assert r0 == {"ts_hour_s": 1782000000, "hs10": 0.821, "hs30": 0.740, "ta": 21.5}
    assert r1 == {"ts_hour_s": 1782003600, "hs10": None, "hs30": 0.741, "ta": -2.5}


def test_coords_golden():
    out = codec.decode_uplink(hexb("02 03 17 86 A3 E6 FF C6 95 3F 00 78"))
    assert out["type"] == "coords"
    assert out["lat"] == pytest.approx(39.4699750)
    assert out["lon"] == pytest.approx(-0.3762881)
    assert out["utc_offset_min"] == 120


def test_cfg_ack_golden():
    assert codec.decode_uplink(hexb("02 04 03 01")) == {
        "type": "cfg_ack", "applied": 3, "rejected": 1,
    }


def test_decode_rejects_bad_version_and_unknown_type():
    with pytest.raises(ValueError):
        codec.decode_uplink(hexb("01 01 00 00"))     # old wire version
    with pytest.raises(ValueError):
        codec.decode_uplink(hexb("02 7E 00"))        # unknown type
    with pytest.raises(ValueError):
        codec.decode_uplink(hexb("02"))              # too short


# --- downlink encode goldens -------------------------------------------------

def test_time_ta_pure_clock_golden():
    # Empty arrays + a clock -> 8-byte pure clock sync.
    assert codec.encode_downlink_time_ta([], [], 1782000000) == hexb("02 01 6A 37 29 80 00 00")


def test_time_ta_with_arrays_and_int8_rounding():
    frame = codec.encode_downlink_time_ta([20.0, -5.4], [22.6], 1)
    # 02 01 | clock=1 | n_past=2 | n_future=1 | 20, -5, 23
    assert frame == hexb("02 01 00 00 00 01 02 01 14 FB 17")


def test_time_ta_rejects_oversized_window():
    with pytest.raises(ValueError):
        codec.encode_downlink_time_ta([0.0] * 49, [], 0)
    with pytest.raises(ValueError):
        codec.encode_downlink_time_ta([], [0.0] * 25, 0)


def test_config_tlv_golden():
    frame = codec.encode_config_patch_tlv({
        "sleep_s": 600, "daily_hour": 21, "inference_mode": 1,
        "utc_offset_min": 120, "irrigation_hour": 6,
    })
    # ascending field id: sleep_s(01) daily_hour(04) inference_mode(06) offset(07) irrigation(08)
    assert frame == hexb("02 02 01 04 00 00 02 58 04 01 15 06 01 01 07 02 00 78 08 01 06")


def test_config_tlv_coords_golden():
    frame = codec.encode_config_patch_tlv({"lat": 39.4699750, "lon": -0.3762881})
    assert frame == hexb("02 02 09 04 17 86 A3 E6 0A 04 FF C6 95 3F")


def test_config_tlv_negative_offset():
    # -300 min via i16 two's complement = 0xFED4 (same as the firmware apply test).
    assert codec.encode_config_patch_tlv({"utc_offset_min": -300}) == hexb("02 02 07 02 FE D4")


# --- downlink TLV range validation -------------------------------------------

@pytest.mark.parametrize("fields", [
    {"sleep_s": 5},              # < 10
    {"sleep_s": 90000},          # > 86400
    {"capture_s": 30},           # < 60
    {"lora_period_s": 100},      # < 300
    {"daily_hour": 24},          # > 23
    {"irrigation_hour": 24},     # > 23
    {"utc_offset_min": -800},    # < -720
    {"utc_offset_min": 900},     # > 840
    {"inference_mode": 2},       # not 0/1
    {"log_level": 3},            # > 2
    {"nope": 1},                 # unknown field
])
def test_config_tlv_out_of_range_raises(fields):
    with pytest.raises(ValueError):
        codec.encode_config_patch_tlv(fields)


@pytest.mark.parametrize("fields", [
    {"sleep_s": 10}, {"sleep_s": 86400},
    {"capture_s": 60}, {"lora_period_s": 300},
    {"daily_hour": 0}, {"daily_hour": 23},
    {"utc_offset_min": -720}, {"utc_offset_min": 840},
])
def test_config_tlv_boundaries_ok(fields):
    assert codec.encode_config_patch_tlv(fields)[:2] == b"\x02\x02"
