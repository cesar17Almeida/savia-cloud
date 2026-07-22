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

    # LoRaWAN profile every station shares (Wio-E5, mirrored from the first node).
    _LORAWAN_VERSION = "MAC_V1_0_2"
    _PHY_VERSION = "PHY_V1_0_2_REV_B"
    _FREQ_PLAN = "EU_863_870_TTN"

    def register_device(self, device_id: str, dev_eui: str, join_eui: str,
                        app_key: str) -> None:
        """Provision an OTAA end device across the Things Stack registries
        (Identity -> Join -> Network -> Application servers)."""
        base = self._s.ttn_base_url
        app = self._s.ttn_app_id
        headers = {"Authorization": f"Bearer {self._s.ttn_api_key}"}
        cluster = base.split("//", 1)[-1]
        ids = {"device_id": device_id,
               "application_ids": {"application_id": app},
               "dev_eui": dev_eui, "join_eui": join_eui}

        steps = [
            ("identity", requests.post, f"{base}/api/v3/applications/{app}/devices", {
                "end_device": {
                    "ids": ids,
                    "join_server_address": cluster,
                    "network_server_address": cluster,
                    "application_server_address": cluster,
                },
                "field_mask": {"paths": [
                    "ids.device_id", "ids.dev_eui", "ids.join_eui",
                    "join_server_address", "network_server_address",
                    "application_server_address",
                ]},
            }),
            ("join", requests.put, f"{base}/api/v3/js/applications/{app}/devices/{device_id}", {
                "end_device": {
                    "ids": ids,
                    "network_server_address": cluster,
                    "application_server_address": cluster,
                    "root_keys": {"app_key": {"key": app_key}},
                },
                "field_mask": {"paths": [
                    "network_server_address", "application_server_address",
                    "root_keys.app_key.key",
                ]},
            }),
            ("network", requests.put, f"{base}/api/v3/ns/applications/{app}/devices/{device_id}", {
                "end_device": {
                    "ids": ids,
                    "lorawan_version": self._LORAWAN_VERSION,
                    "lorawan_phy_version": self._PHY_VERSION,
                    "frequency_plan_id": self._FREQ_PLAN,
                    "supports_join": True,
                },
                "field_mask": {"paths": [
                    "lorawan_version", "lorawan_phy_version",
                    "frequency_plan_id", "supports_join",
                ]},
            }),
            ("application", requests.put, f"{base}/api/v3/as/applications/{app}/devices/{device_id}", {
                "end_device": {"ids": ids},
                "field_mask": {"paths": []},
            }),
        ]
        for name, verb, url, body in steps:
            resp = verb(url, json=body, headers=headers, timeout=10)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"TTN {name}-server registration failed "
                    f"({resp.status_code}): {resp.text[:300]}"
                )
