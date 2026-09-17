"""TTN webhook: shared-secret gating, link-quality capture, and soil upsert."""
import base64

from app.adapters.repository.memory import InMemoryStationRepository
from app.adapters.ttn import codec
from app.application.services import IngestUplinkService
from app.domain.ports import ReadingRepository
from tests.conftest import WEBHOOK_SECRET

SOIL_FRAME = bytes.fromhex(
    "02 02 02 6A 37 29 80 03 35 02 E4 00 D7 "
    "6A 37 37 90 FF FF 02 E5 FF E7".replace(" ", "")
)
COORDS_FRAME = bytes.fromhex("02 03 17 86 A3 E6 FF C6 95 3F 00 78".replace(" ", ""))
BOOT_FRAME = bytes.fromhex("02 06 6A 45 01 40".replace(" ", ""))


def _ttn_body(dev="EUI-W", payload=SOIL_FRAME):
    return {
        "end_device_ids": {"device_id": dev},
        "uplink_message": {
            "f_port": 8,
            "frm_payload": base64.b64encode(payload).decode(),
            "rx_metadata": [{"rssi": -100, "snr": 7.5}, {"rssi": -90, "snr": 9.0}],
        },
    }


def test_webhook_rejects_missing_or_wrong_token(client):
    assert client.post("/ttn/uplink", json=_ttn_body()).status_code == 401
    assert client.post("/ttn/uplink", json=_ttn_body(),
                       headers={"X-Webhook-Token": "nope"}).status_code == 401


def test_webhook_accepts_and_records_signal(client):
    r = client.post("/ttn/uplink", json=_ttn_body(),
                    headers={"X-Webhook-Token": WEBHOOK_SECRET})
    assert r.status_code == 200 and r.get_json()["type"] == "soil"

    sig = client.get("/stations/EUI-W/signal").get_json()["signal"]
    assert sig["rssi_dbm"] == -90 and sig["snr_db"] == 9.0  # best-RSSI gateway wins


class _FakeReadings(ReadingRepository):
    def __init__(self):
        self.rows = []

    def upsert_soil(self, reading):
        self.rows.append(reading)

    def window(self, dev_eui, from_ts, to_ts):
        return []

    def recent(self, dev_eui, limit):
        return []


def test_ingest_service_upserts_soil_records():
    stations = InMemoryStationRepository()
    readings = _FakeReadings()
    svc = IngestUplinkService(stations, readings, default_lat=39.47, default_lon=-0.38)

    decoded = codec.decode_uplink(SOIL_FRAME)
    svc.handle("EUI-Z", decoded, -80, 8.0, 1782000000)

    assert len(readings.rows) == 2
    assert readings.rows[0].ts_hour_s == 1782000000 and readings.rows[0].hs10 == 0.821
    assert readings.rows[1].hs10 is None and readings.rows[1].hs30 == 0.741
    st = stations.get("EUI-Z")
    assert st.last_rssi == -80 and st.last_snr == 8.0 and st.last_uplink_at == 1782000000


def test_ingest_service_updates_coords():
    stations = InMemoryStationRepository()
    svc = IngestUplinkService(stations, _FakeReadings())
    decoded = codec.decode_uplink(COORDS_FRAME)
    svc.handle("EUI-C", decoded, -70, 6.0, 111)
    st = stations.get("EUI-C")
    assert round(st.lat, 6) == 39.469975 and st.utc_offset_min == 120


def test_uplink_queues_clock_sync_at_most_every_6h(client, ttn_capture):
    """Any uplink refreshes the station clock if none went out in 6 h; a second
    uplink right after must NOT queue another one (TTN fair use)."""
    body = _ttn_body()
    client.post("/ttn/uplink", json=body, headers={"X-Webhook-Token": WEBHOOK_SECRET})
    assert len(ttn_capture) == 1                      # 8-byte pure clock sync
    import base64 as b64
    payload = b64.b64decode(ttn_capture[0]["json"]["downlinks"][0]["frm_payload"])
    assert payload[:2] == bytes([0x02, 0x01]) and len(payload) == 8
    assert payload[6] == 0 and payload[7] == 0        # no TA arrays

    client.post("/ttn/uplink", json=body, headers={"X-Webhook-Token": WEBHOOK_SECRET})
    assert len(ttn_capture) == 1                      # still just one


def test_boot_uplink_forces_clock_sync(client, ttn_capture):
    """A BOOT frame is the node's first uplink after power-up: it has no clock, so
    the backend answers with the time in that RX window even if a sync went out
    minutes ago."""
    import base64 as b64
    hdr = {"X-Webhook-Token": WEBHOOK_SECRET}
    client.post("/ttn/uplink", json=_ttn_body(), headers=hdr)
    assert len(ttn_capture) == 1                      # routine 6 h sync
    client.post("/ttn/uplink", json=_ttn_body(payload=BOOT_FRAME), headers=hdr)
    assert len(ttn_capture) == 2                      # boot bypasses the gap
    payload = b64.b64decode(ttn_capture[1]["json"]["downlinks"][0]["frm_payload"])
    assert payload[:2] == bytes([0x02, 0x01]) and len(payload) == 8


def test_cfg_ack_closes_the_oldest_queued_config(client, ttn_capture):
    """TTN drains its queue FIFO: one CFG_ACK confirms the first config pushed and
    the second one stays pending."""
    hdr = {"X-Webhook-Token": WEBHOOK_SECRET}
    client.post("/ttn/uplink", json=_ttn_body(dev="EUI-Q"), headers=hdr)
    svc = client.application.config["SERVICES"]
    svc.config_downlink.run("EUI-Q", {"sleep_s": 100}, 1_789_000_000)
    svc.config_downlink.run("EUI-Q", {"sleep_s": 200}, 1_789_000_060)

    ack = bytes([codec.VERSION, codec.UP_CFG_ACK, 1, 0])
    client.post("/ttn/uplink", json=_ttn_body(dev="EUI-Q", payload=ack), headers=hdr)
    configs = [d for d in svc.panel.downlinks("EUI-Q", 10) if d.kind == "config"]
    assert [d.state for d in configs] == ["scheduled", "applied"]   # newest first
    assert configs[1].payload_hex.endswith("00000064")               # sleep_s=100
    assert svc.panel.config_state("EUI-Q")["state"] == "scheduled"


def test_failed_clock_sync_is_retried_on_the_next_uplink(client, monkeypatch):
    import app.adapters.ttn.client as ttn_client

    pushes = []

    def _flaky(self, command):
        pushes.append(command)
        if len(pushes) == 1:
            raise RuntimeError("TTN unreachable")
    monkeypatch.setattr(ttn_client.TtnHttpClient, "schedule_downlink", _flaky)

    hdr = {"X-Webhook-Token": WEBHOOK_SECRET}
    client.post("/ttn/uplink", json=_ttn_body(), headers=hdr)   # push fails
    client.post("/ttn/uplink", json=_ttn_body(), headers=hdr)   # retried at once
    assert len(pushes) == 2
    client.post("/ttn/uplink", json=_ttn_body(), headers=hdr)   # inside the 6 h gap
    assert len(pushes) == 2


def test_implausible_soil_timestamps_are_not_stored():
    """Clockless or corrupted buckets stay in the raw uplink log only."""
    readings = _FakeReadings()
    svc = IngestUplinkService(InMemoryStationRepository(), readings)
    at = 1_789_000_000
    rec = {"hs10": 0.5, "hs30": 0.5, "ta": 20.0}
    decoded = {"type": "soil", "records": [
        {"ts_hour_s": 0, **rec},
        {"ts_hour_s": 0xFFFFFFFF, **rec},
        {"ts_hour_s": at + 2 * 86400, **rec},
        {"ts_hour_s": at - 3600, **rec},
    ]}
    svc.handle("EUI-T", decoded, -80, 8.0, at)
    assert [r.ts_hour_s for r in readings.rows] == [at - 3600]


def test_webhook_survives_an_out_of_range_timestamp(client):
    """A u32 timestamp above the int32 range must not reach PostgreSQL."""
    frame = bytes([codec.VERSION, codec.UP_SOIL, 1]) + bytes.fromhex("FFFFFFFF" "0320" "0300" "00C8")
    r = client.post("/ttn/uplink", json=_ttn_body(dev="EUI-BIG", payload=frame),
                    headers={"X-Webhook-Token": WEBHOOK_SECRET})
    assert r.status_code == 200 and r.get_json()["type"] == "soil"
