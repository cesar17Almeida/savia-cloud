"""TTN adapter: schedule downlinks via The Things Stack Application Server HTTP API.

Uplinks flow the other way (TTN -> our webhook, see interfaces/http/routes.py), so
this outbound client only handles downlinks.
"""
from __future__ import annotations

import base64

import requests

from config import Settings

from ...domain.models import DownlinkCommand
from ...domain.ports import TtnPort


class TtnHttpClient(TtnPort):
    def __init__(self, settings: Settings):
        self._s = settings

    def schedule_downlink(self, command: DownlinkCommand) -> None:
        url = (
            f"{self._s.ttn_base_url}/api/v3/as/applications/"
            f"{self._s.ttn_app_id}/devices/{command.dev_id}/down/push"
        )
        body = {
            "downlinks": [
                {
                    "f_port": command.f_port,
                    "frm_payload": base64.b64encode(command.payload).decode(),
                    "priority": "NORMAL",
                    "confirmed": command.confirmed,
                }
            ]
        }
        headers = {"Authorization": f"Bearer {self._s.ttn_api_key}"}
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"TTN downlink push failed ({resp.status_code}): {resp.text[:300]}"
            )
