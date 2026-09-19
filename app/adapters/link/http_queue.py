"""HTTP link adapter: the LoRa hop replaced by a tunnel through the phone.

Same TtnPort as the TTN client, but a downlink is parked in a DB outbox until the
station's next uplink arrives on POST /link/uplink (class-A: a downlink only ever
travels as the answer to an uplink). The payload bytes are the wire-v2 frames.
"""
from __future__ import annotations

import time
from typing import Callable

from ...domain.models import DownlinkCommand
from ...domain.ports import LinkOutboxRepository, TtnPort


class HttpLinkGateway(TtnPort):
    def __init__(self, outbox: LinkOutboxRepository,
                 clock: Callable[[], float] = time.time):
        self._outbox = outbox
        self._clock = clock

    def schedule_downlink(self, command: DownlinkCommand) -> None:
        """Queue for the next RX window; there is no upstream that could refuse it."""
        self._outbox.add(command.dev_id, command.f_port, command.payload,
                         int(self._clock()))

    def register_device(self, device_id: str, dev_eui: str, join_eui: str,
                        app_key: str) -> None:
        """Nothing to provision: the tunnel has no network server."""
