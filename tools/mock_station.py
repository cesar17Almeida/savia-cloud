"""Simulate a station against a LIVE deployment: backdated wire-v2 SOIL uplinks
through the real TTN webhook, so the panel fills with mock data end-to-end.

Usage:
  python tools/mock_station.py --base-url https://<host> --secret <WEBHOOK_SECRET> \
      [--dev-eui savia-mock] [--hours 48]

Same mock generator as tests/test_e2e_mock.py (drying curve + daily TA cycle).
After seeding, trigger the inference from the panel ("Inferir ahora") or via
POST /cron/daily {"force": true} with the cron secret.
"""
from __future__ import annotations

import argparse
import base64
import math
import struct
import time

import requests

HOUR = 3600
VERSION, UP_SOIL = 0x02, 0x02


def mock_soil(h: int) -> tuple[float, float, float]:
    hs10 = 0.80 - 0.0008 * (48 - h)
    hs30 = 0.78 - 0.0005 * (48 - h)
    ta = 24 + 5 * math.sin(2 * math.pi * (h % 24) / 24)
    return hs10, hs30, ta


def soil_frame(records) -> bytes:
    out = bytearray([VERSION, UP_SOIL, len(records)])
    for ts, hs10, hs30, ta in records:
        out += struct.pack(">IHHh", ts, round(hs10 * 1000), round(hs30 * 1000),
                           round(ta * 10))
    return bytes(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--secret", required=True, help="WEBHOOK_SECRET (X-Webhook-Token)")
    ap.add_argument("--dev-eui", default="savia-mock")
    ap.add_argument("--hours", type=int, default=48)
    args = ap.parse_args()

    latest = int(time.time()) // HOUR * HOUR
    recs = []
    for h in range(args.hours, 0, -1):
        hs10, hs30, ta = mock_soil(h)
        recs.append((latest - (h - 1) * HOUR, hs10, hs30, ta))

    sent = 0
    for i in range(0, len(recs), 4):
        frame = soil_frame(recs[i:i + 4])
        r = requests.post(f"{args.base_url}/ttn/uplink", json={
            "end_device_ids": {"device_id": args.dev_eui},
            "uplink_message": {
                "frm_payload": base64.b64encode(frame).decode(),
                "rx_metadata": [{"rssi": -117, "snr": -8.5}],
            },
        }, headers={"X-Webhook-Token": args.secret}, timeout=15)
        r.raise_for_status()
        sent += 1
        print(f"uplink {sent}: {len(frame)} B, {r.json()}")

    print(f"\nSeeded {len(recs)} hourly records over {sent} uplinks "
          f"for '{args.dev_eui}'. Now run the inference from the panel.")


if __name__ == "__main__":
    main()
