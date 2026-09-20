"""Send queue: what the HTTP link still owes a station, and the two controls the
operator has over it -- holding the queue and withdrawing one frame.

Only the HTTP link has a queue the backend owns; over TTN the frames live in the
Things Stack queue, and the page says so instead of offering controls that would
not reach them.
"""
from __future__ import annotations

import base64
import time
from dataclasses import replace

import pytest

from app.adapters.ttn import codec
from app.domain.models import DL_CANCELLED, DL_DELIVERED, DL_QUEUED
from app.factory import create_app
from tests.conftest import ADMIN_PASSWORD

DEV = "savia-estacion-01"
# A BOOT enrols the station AND earns it an automatic clock downlink, so every
# later uplink here is the plain RX-window request the station really polls with.
BOOT = bytes.fromhex("020600000000")
POLL = bytes([codec.VERSION, codec.UP_FORECAST, 0xFF, 0xFF])


@pytest.fixture
def link_client(settings):
    return create_app(replace(settings, link_mode="http",
                              forecast_source="dataset")).test_client()


def _up(client, frame: bytes = POLL, dev: str = DEV):
    return client.post("/link/uplink", json={
        "device_id": dev, "f_port": 8,
        "frm_payload": base64.b64encode(frame).decode()})


def _enrol(client, dev: str = DEV):
    """First contact: the station appears and takes its clock in the same answer."""
    return _up(client, BOOT, dev)


def _login(client):
    client.post("/home/login", data={"user": "admin", "password": ADMIN_PASSWORD})


def _svc(client):
    return client.application.config["SERVICES"]


def _enqueue(client, dev: str = DEV, **fields):
    """One config downlink parked in the outbox, like the panel's own form does."""
    return _svc(client).config_downlink.run(dev, fields or {"lora_period_s": 600},
                                            int(time.time()))


def _queued(client, dev: str = DEV):
    return _svc(client).link_queue.pending(dev)


def _page(client) -> str:
    return client.get("/home/queue").get_data(as_text=True)


# --- the page ------------------------------------------------------------------

def test_queue_lists_what_is_waiting_with_its_contents(link_client):
    """A queued frame is listed decoded, not as a blob: the operator can see what
    would reach the station before deciding whether to let it."""
    _enrol(link_client)
    _login(link_client)
    _enqueue(link_client, lora_period_s=600, utc_offset_min=120)

    page = _page(link_client)
    assert "Cola de envíos" in page
    assert "config" in page
    assert "lora_period_s = 600" in page          # decoded TLV, not just hex
    assert "utc_offset_min = 120" in page
    assert "siguiente en salir" in page           # which one leaves first


def test_empty_queue_says_so(link_client):
    _enrol(link_client)
    _login(link_client)
    page = _page(link_client)
    assert "La cola está vacía" in page
    assert "Cancelar" not in page


def test_over_ttn_the_page_explains_the_queue_is_not_ours(client):
    """LINK_MODE=ttn: the frames were handed to The Things Stack when they were
    scheduled, so the page must not pretend the backend can hold them."""
    _login(client)
    page = _page(client)
    assert "The Things Stack" in page
    assert "Retener" not in page and "Cancelar" not in page


# --- holding the queue ----------------------------------------------------------

def test_pausing_holds_every_frame_until_resumed(link_client):
    """Held, the queue keeps filling and hands out nothing; resumed, the oldest
    frame leaves first -- the hold must not reorder the queue."""
    _enrol(link_client)
    _login(link_client)
    _enqueue(link_client, lora_period_s=600)
    first = _queued(link_client)[0].payload_hex

    link_client.post(f"/home/queue/{DEV}/pause", data={"paused": "1"})
    assert _up(link_client).get_json()["downlink"] is None    # nothing goes out
    assert len(_queued(link_client)) == 1                     # and nothing is lost

    # A second frame queued while held simply joins the line.
    _enqueue(link_client, capture_s=3600)
    assert len(_queued(link_client)) == 2
    assert _up(link_client).get_json()["downlink"] is None

    link_client.post(f"/home/queue/{DEV}/pause", data={"paused": "0"})
    out = _up(link_client).get_json()["downlink"]
    assert out is not None
    assert base64.b64decode(out["frm_payload"]).hex() == first   # FIFO kept


def test_a_held_station_with_nothing_queued_is_still_listed(link_client):
    """Otherwise the only control that could release it would have nowhere to live."""
    _enrol(link_client)
    _login(link_client)
    link_client.post(f"/home/queue/{DEV}/pause", data={"paused": "1"})

    page = _page(link_client)
    assert DEV in page
    assert "Reanudar" in page
    assert "No queda nada en cola" in page


def test_pausing_an_unknown_station_is_refused(link_client):
    """A typo in the URL must not create a hold nobody can find again."""
    _login(link_client)
    resp = link_client.post("/home/queue/nope/pause", data={"paused": "1"},
                            follow_redirects=True)
    assert "No existe ninguna estación" in resp.get_data(as_text=True)
    assert _svc(link_client).link_queue.paused() == {}


# --- withdrawing one frame -------------------------------------------------------

def test_cancelling_keeps_the_frame_from_ever_reaching_the_station(link_client):
    """The queue entry goes and the logged downlink says cancelled, so the history
    still records that the frame existed and why it never left."""
    _enrol(link_client)
    _login(link_client)
    _enqueue(link_client, lora_period_s=600)
    item = _queued(link_client)[0]

    resp = link_client.post(f"/home/queue/{item.id}/cancel", follow_redirects=True)
    assert "Paquete cancelado" in resp.get_data(as_text=True)
    assert _queued(link_client) == []
    assert _up(link_client).get_json()["downlink"] is None      # never delivered

    logged = _svc(link_client).panel.downlinks(DEV, 5)
    assert logged[0].kind == "config" and logged[0].state == DL_CANCELLED
    assert "retirado de la cola" in logged[0].status


def test_cancelling_only_touches_its_own_frame(link_client):
    """Two queued frames, one cancelled: the other still leaves, in its turn."""
    _enrol(link_client)
    _login(link_client)
    _enqueue(link_client, lora_period_s=600)
    _enqueue(link_client, capture_s=3600)
    doomed, survivor = _queued(link_client)

    link_client.post(f"/home/queue/{doomed.id}/cancel")
    assert [q.id for q in _queued(link_client)] == [survivor.id]

    out = _up(link_client).get_json()["downlink"]
    assert base64.b64decode(out["frm_payload"]).hex() == survivor.payload_hex


def test_cancelling_something_already_delivered_says_so(link_client):
    """No silent no-op: the operator is told the frame had already gone."""
    _enrol(link_client)
    _login(link_client)
    _enqueue(link_client, lora_period_s=600)
    item = _queued(link_client)[0]
    assert _up(link_client).get_json()["downlink"] is not None   # it leaves

    resp = link_client.post(f"/home/queue/{item.id}/cancel", follow_redirects=True)
    assert "ya no estaba en la cola" in resp.get_data(as_text=True)
    # The delivery stands: cancelling after the fact must not rewrite history.
    logged = _svc(link_client).panel.downlinks(DEV, 5)
    assert logged[0].state == DL_DELIVERED


def test_queue_controls_are_refused_over_ttn(client):
    """The backend cannot reach the Things Stack queue, and says so instead of
    reporting a success that never happened."""
    _login(client)
    resp = client.post(f"/home/queue/{DEV}/pause", data={"paused": "1"},
                       follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "The Things Stack" in html
    assert 'class="toast toast-error"' in html
    assert client.post("/home/queue/1/cancel",
                       follow_redirects=True).status_code == 200


def test_a_paused_station_does_not_stop_the_others(link_client):
    """The hold is per station: the queue is FIFO per device, so holding one must
    not block a neighbour that is waiting for the same kind of frame."""
    other = "savia-estacion-02"
    _enrol(link_client)
    _enrol(link_client, other)
    _login(link_client)
    _enqueue(link_client, lora_period_s=600)
    _enqueue(link_client, other, lora_period_s=900)

    link_client.post(f"/home/queue/{DEV}/pause", data={"paused": "1"})
    assert _up(link_client).get_json()["downlink"] is None
    assert _up(link_client, dev=other).get_json()["downlink"] is not None
    assert [q.dev_id for q in _svc(link_client).link_queue.pending()] == [DEV]


def test_the_queued_count_the_panel_shows_ignores_held_state(link_client):
    """Holding does not empty the queue; the station page keeps reporting what is
    still owed, which is what the operator went to the queue page to act on."""
    _enrol(link_client)
    _login(link_client)
    _enqueue(link_client, lora_period_s=600)
    link_client.post(f"/home/queue/{DEV}/pause", data={"paused": "1"})

    assert _svc(link_client).link_outbox.pending(DEV) == 1
    assert _svc(link_client).panel.downlinks(DEV, 1)[0].state == DL_QUEUED
