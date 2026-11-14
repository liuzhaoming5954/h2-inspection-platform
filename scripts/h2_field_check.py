#!/usr/bin/env python
"""Field self-check for the H2 teleop chain: acquire, heartbeat, release.

Exercises the real path (browser-style WebSocket -> backend -> SSH forward ->
PC3 gateway -> lease renewal) without sending any motion command. Run from
the workstation after h2_field_start.sh; exit code 0 means the chain is
ready for teleoperation, 1 means do not hand control to an operator.

Usage: .venv/bin/python scripts/h2_field_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets

WS_URL = "ws://127.0.0.1:8000/api/teleop/ws"
ORIGIN = "http://127.0.0.1:5173"
HEARTBEATS = 5
HEARTBEAT_INTERVAL = 0.4


async def run_check() -> tuple[bool, list[str]]:
    log: list[str] = []
    renewed = 0
    async with websockets.connect(WS_URL, additional_headers={"Origin": ORIGIN}) as ws:
        sequence = 0
        deadline = time.monotonic() + 2 + HEARTBEATS * HEARTBEAT_INTERVAL + 2
        while time.monotonic() < deadline:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.3))
            except asyncio.TimeoutError:
                if renewed >= HEARTBEATS:
                    break
                continue
            kind, code = message.get("type"), message.get("code")
            if kind == "lease" and code == "acquired":
                log.append("lease acquired")
            elif kind == "ack" and code == "renewed":
                renewed += 1
                if renewed == 1:
                    # First renewal starts the heartbeat cadence.
                    pass
            elif kind in ("error", "ack") and message.get("accepted") is False:
                log.append(f"REJECTED {kind}/{code}: {message.get('message')}")
                return False, log
            elif kind == "lease" and message.get("accepted") is False:
                log.append(f"DENIED {code}: {message.get('message')}")
                return False, log
            # Send the next heartbeat when due (single paced sender).
            if renewed < HEARTBEATS and kind in ("lease", "ack"):
                sequence += 1
                await ws.send(json.dumps({
                    "action": "heartbeat", "sequence": sequence,
                    "sent_at_ms": int(time.time() * 1000),
                }))
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        ok = renewed >= HEARTBEATS
        if ok:
            await ws.send(json.dumps({
                "action": "release", "sequence": sequence + 1,
                "sent_at_ms": int(time.time() * 1000),
            }))
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                log.append(f"released: {message.get('code')}")
            except (asyncio.TimeoutError, Exception):
                log.append("release ack not observed (lease expires anyway)")
        return ok, log


def main() -> None:
    try:
        ok, log = asyncio.run(run_check())
    except Exception as error:
        print(f"FAIL: cannot reach the teleop chain: {type(error).__name__}: {error}")
        sys.exit(1)
    for line in log:
        print(f"  {line}")
    if ok:
        print("SELF-CHECK PASSED: lease + heartbeat renewal work; teleop chain is ready.")
    else:
        print("SELF-CHECK FAILED: heartbeats were not renewed. Check clock alignment "
              "(host vs PC3) and the gateway log before touching the robot.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
