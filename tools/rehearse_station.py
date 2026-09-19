#!/usr/bin/env python3
"""Rehearse the HTTP link without the board or the phone.

Plays the station AND the TerraLink gateway against a savia-cloud started with
`make demo`: it posts the frames the firmware would send (wire v2, same bytes) to
`POST /link/uplink`, prints every downlink that comes back and reacts to it the
way the station does:

  BOOT                 first frame; the backend answers with the clock
  FORECAST (unknown)   every --period seconds, keeps the RX window open
  TIME_TA downlink     TA window complete -> "inference" -> FORECAST with hs30_min
  CONFIG downlink      -> CFG_ACK with the number of fields
  --forward            uplinks the 48 h soil backlog as SOIL frames first

The soil values and the hs30_min come from the same training-set replay the
firmware embeds (app/adapters/dataset/replay_2018.json), anchored the same way,
so what the panel shows here is what it shows with the real station.

Usage:
    python tools/rehearse_station.py                       # http://127.0.0.1:8000
    python tools/rehearse_station.py --base-url http://192.168.1.20:8000 --forward

Standard library only.
"""
import argparse
import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

VERSION = 0x02
UP_FORECAST, UP_SOIL, UP_CFG_ACK, UP_BOOT = 0x01, 0x02, 0x04, 0x06
DN_TIME_TA, DN_CONFIG = 0x01, 0x02
HS_UNKNOWN = 0xFFFF
TA_UNKNOWN = 0x7FFF

HERE = os.path.dirname(os.path.abspath(__file__))
REPLAY = os.path.join(HERE, "..", "app", "adapters", "dataset", "replay_2018.json")


def load_replay():
    with open(REPLAY) as fh:
        return json.load(fh)


def row_now(replay, now_s, offset_min):
    hod = ((now_s + offset_min * 60) // 3600) % 24
    return replay["base_row"] + (hod - replay["base_hour"]) % 24


def frame_boot():
    return struct.pack(">BBI", VERSION, UP_BOOT, 0)


def frame_forecast(hs30_min=None):
    raw = HS_UNKNOWN if hs30_min is None else max(0, min(0xFFFE, int(hs30_min * 1000 + 0.5)))
    return struct.pack(">BBH", VERSION, UP_FORECAST, raw)


def frame_cfg_ack(applied, rejected=0):
    return struct.pack(">BBBB", VERSION, UP_CFG_ACK, applied, rejected)


def frame_soil(records):
    body = b"".join(struct.pack(">IHHh", ts, int(h10 * 1000 + 0.5), int(h30 * 1000 + 0.5), TA_UNKNOWN)
                    for ts, h10, h30 in records)
    return struct.pack(">BBB", VERSION, UP_SOIL, len(records)) + body


def describe_downlink(data):
    if len(data) < 2 or data[0] != VERSION:
        return "desconocido", {}
    if data[1] == DN_TIME_TA and len(data) >= 8:
        clock, n_past, n_future = struct.unpack(">IBB", data[2:8])
        ta = [b - 256 if b > 127 else b for b in data[8:8 + n_past + n_future]]
        return "time_ta", {"clock": clock, "past": ta[:n_past], "future": ta[n_past:]}
    if data[1] == DN_CONFIG:
        fields, i = 0, 2
        while i + 2 <= len(data):
            i += 2 + data[i + 1]
            fields += 1
        return "config", {"fields": fields}
    return "desconocido", {}


class Link:
    def __init__(self, base_url, device, token, offset_min):
        self.url = base_url.rstrip("/") + "/link/uplink"
        self.device, self.token, self.offset_min = device, token, offset_min
        self.seq = 0

    def uplink(self, frame, label):
        self.seq += 1
        body = json.dumps({"device_id": self.device, "f_port": 8, "seq": self.seq,
                           "frm_payload": base64.b64encode(frame).decode(),
                           "utc_offset_min": self.offset_min}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("X-Link-Token", self.token)
        stamp = time.strftime("%H:%M:%S")
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"{stamp}  ^ {label:<22} {frame.hex()}  -> HTTP {e.code}: {e.read()[:120]!r}")
            return None
        except OSError as e:
            print(f"{stamp}  ^ {label:<22} {frame.hex()}  -> sin respuesta ({e})")
            return None
        dn = reply.get("downlink")
        print(f"{stamp}  ^ {label:<22} {len(frame):2d} B {frame.hex()}")
        if not dn:
            return b""
        data = base64.b64decode(dn["frm_payload"])
        kind, info = describe_downlink(data)
        extra = ""
        if kind == "time_ta":
            extra = f"  reloj={info['clock']}  TA {len(info['past'])}+{len(info['future'])}"
            if info["past"]:
                ta = info["past"] + info["future"]
                extra += f"  ({min(ta)}..{max(ta)} degC)"
        elif kind == "config":
            extra = f"  {info['fields']} campos"
        print(f"{stamp}  v {kind:<22} {len(data):2d} B {data.hex()[:48]}{'...' if len(data) > 24 else ''}{extra}")
        return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--device", default="savia-estacion-01")
    ap.add_argument("--token", default="")
    ap.add_argument("--period", type=float, default=5.0, help="seconds between uplinks (the station: 15)")
    ap.add_argument("--offset-min", type=int, default=120, help="replay UTC offset (REPLAY_UTC_OFFSET_MIN)")
    ap.add_argument("--forward", action="store_true", help="uplink the 48 h soil backlog (mode FORWARD)")
    ap.add_argument("--cycles", type=int, default=0, help="stop after N uplinks (0 = until Ctrl-C)")
    args = ap.parse_args()

    replay = load_replay()
    link = Link(args.base_url, args.device, args.token, args.offset_min)
    print(f"estacion {args.device} -> {link.url}   (Ctrl-C para salir)")

    pending = [("arranque (BOOT)", frame_boot())]
    if args.forward:
        now_s = int(time.time())
        hour = now_s - now_s % 3600
        row = row_now(replay, now_s, args.offset_min)
        recs = [(hour - (47 - k) * 3600, replay["hs10"][row - 47 + k], replay["hs30"][row - 47 + k])
                for k in range(48)]
        for k in range(0, 48, 4):
            pending.append((f"humedad {k + 1}-{k + 4}/48 h", frame_soil(recs[k:k + 4])))

    hs30_min, sent = None, 0
    try:
        while True:
            label, frame = pending.pop(0) if pending else (
                "pronostico" if hs30_min is not None else "sin pronostico", frame_forecast(hs30_min))
            data = link.uplink(frame, label)
            sent += 1
            if data:
                kind, info = describe_downlink(data)
                if kind == "time_ta" and len(info["past"]) == 48 and len(info["future"]) == 24:
                    row = row_now(replay, int(time.time()), args.offset_min)
                    hs30_min = min(replay["hs30"][row + 1:row + 25]) + 0.003   # what the model lands on
                    print(f"          TA 72/72 -> la estacion infiere: HS30 minimo previsto {hs30_min:.3f}")
                    pending.insert(0, ("pronostico (resultado)", frame_forecast(hs30_min)))
                elif kind == "config":
                    pending.insert(0, ("confirmacion config", frame_cfg_ack(info["fields"])))
            if args.cycles and sent >= args.cycles:
                return 0
            time.sleep(1.0 if pending else args.period)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
