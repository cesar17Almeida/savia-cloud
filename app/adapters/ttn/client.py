"""TTN adapter: schedule downlinks via The Things Stack Application Server HTTP API.

Uplinks flow the other way (TTN -> our webhook, see interfaces/http/routes.py), so
this outbound client only handles downlinks.
"""
from __future__ import annotations

import base64

# import requests  # enable when wiring the real HTTP call

from config import Settings

from ...domain.models import DownlinkCommand
from ...domain.ports import TtnPort


class TtnHttpClient(TtnPort):
    def __init__(self, settings: Settings):
        self._s = settings

    def schedule_downlink(self, command: DownlinkCommand) -> None:
        # POST {base}/api/v3/as/applications/{app}/devices/{dev}/down/push
        # headers: Authorization: Bearer <TTN_API_KEY>
        # body: {"downlinks": [{"f_port": N, "frm_payload": "<base64>", "priority": "NORMAL"}]}
        _url = (
            f"{self._s.ttn_base_url}/api/v3/as/applications/"
            f"{self._s.ttn_app_id}/devices/{command.dev_id}/down/push"
        )
        _body = {
            "downlinks": [
                {
                    "f_port": command.f_port,
                    "frm_payload": base64.b64encode(command.payload).decode(),
                    "priority": "NORMAL",
                    "confirmed": command.confirmed,
                }
            ]
        }
        raise NotImplementedError("TODO: requests.post(_url, json=_body, headers=Bearer)")
