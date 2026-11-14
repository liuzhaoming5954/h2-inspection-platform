#!/usr/bin/env python
"""Field self-check for the H2 teleop chain: acquire, heartbeat, release.

Checks hardware mode and readiness, then exercises the WebSocket and the PC3
gateway's lease path without sending any motion command. Run on the workstation.

Usage: .venv/bin/python scripts/h2_field_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from urllib.request import ProxyHandler, build_opener

import websockets

API_URL = "http://127.0.0.1:8000/api"
WS_URL = "ws://127.0.0.1:8000/api/teleop/ws"
ORIGIN = "http://127.0.0.1:5173"
HEARTBEATS = 5
HEARTBEAT_INTERVAL = 0.4
REPLY_TIMEOUT = 2


def hardware_is_ready() -> bool:
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{API_URL}/health", timeout=REPLY_TIMEOUT) as response:
        health = json.load(response)
    if health.get("hardware_teleop") is not True or health.get("gateway") != "connected":
        return False
    with opener.open(f"{API_URL}/robot", timeout=REPLY_TIMEOUT) as response:
        state = json.load(response)
    return (state.get("simulated") is False and state.get("teleop_ready") is True
            and state.get("teleop_active") is False and state.get("status") == "ready")


async def run_check() -> tuple[bool, list[str]]:
    log: list[str] = []
    async with websockets.connect(WS_URL, additional_headers={"Origin": ORIGIN}) as ws:
        lease = json.loads(await asyncio.wait_for(ws.recv(), timeout=REPLY_TIMEOUT))
        if lease.get("type") != "lease" or lease.get("accepted") is not True or lease.get("code") != "acquired":
            return False, [f"lease: {lease.get('code')}"]
        log.append("lease acquired")

        async def acknowledge(action: str, sequence: int, expected: str) -> bool:
            await ws.send(json.dumps({
                "action": action, "sequence": sequence,
                "sent_at_ms": time.time_ns() // 1_000_000,
            }))
            try:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=REPLY_TIMEOUT))
            except (TimeoutError, websockets.ConnectionClosed):
                log.append(f"{action}: no acknowledgement")
                return False
            log.append(f"{action}: {reply.get('code')}")
            return (reply.get("type") == "ack" and reply.get("accepted") is True
                    and reply.get("code") == expected and reply.get("sequence") == sequence)

        for sequence in range(1, HEARTBEATS + 1):
            if not await acknowledge("heartbeat", sequence, "renewed"):
                return False, log
            await asyncio.sleep(HEARTBEAT_INTERVAL)
        return await acknowledge("release", HEARTBEATS + 1, "released"), log


def main() -> None:
    try:
        if not hardware_is_ready():
            print("FAIL: true hardware mode and an available, idle H2 are required.")
            sys.exit(1)
        ok, log = asyncio.run(run_check())
    except Exception as error:
        print(f"FAIL: cannot reach the teleop chain: {type(error).__name__}: {error}")
        sys.exit(1)
    for line in log:
        print(f"  {line}")
    if ok:
        print("SELF-CHECK PASSED: hardware ready, lease acquired, heartbeats acknowledged, lease released.")
    else:
        print("SELF-CHECK FAILED: check the local API, SSH forward and PC3 gateway log.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
