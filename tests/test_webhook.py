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


def test_ingest_service_upserts_soil_records():
    stations = InMemoryStationRepository()
    readings = _FakeReadings()
    svc = IngestUplinkService(stations, readings, 39.47, -0.38)

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
