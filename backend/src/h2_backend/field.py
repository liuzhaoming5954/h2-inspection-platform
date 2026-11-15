"""Field-link management: manual probes and SSH actions for the teleop chain.

The workstation owns three removable links: the PC2 sensor agent, the PC3
gateway process, and the two SSH forwards that carry them here. Nothing in
this module runs automatically - the UI exposes buttons and a human clicks
them. Every action is idempotent (already-running components are reported,
not restarted) and never sends motion commands.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request

PC2 = os.environ.get("H2_PC2_HOST", "192.168.124.162")
PC3 = os.environ.get("H2_PC3_HOST", "192.168.124.163")
PC2_AGENT_URL = f"http://{PC2}:8090/environment"
FORWARD_PORTS = (15051, 15052)

PC2_START = "PYTHONPATH=$HOME/pylibs nohup python3 ~/sensor_agent.py > ~/sensor_agent.log 2>&1 < /dev/null &"
PC3_START = ("cd ~/h2-inspection-platform && CYCLONEDDS_HOME=$PWD/.runtime/h2-sdk2/cyclonedds-install "
             "nohup .venv/bin/python -u -m h2_gateway.server --h2-interface eth10 "
             "--host 127.0.0.1 --port 50051 --teleop-port 50052 > gateway.log 2>&1 < /dev/null &")


class FieldError(RuntimeError):
    pass


def _password() -> str:
    password = os.environ.get("H2_FIELD_PASSWORD")
    if not password:
        raise FieldError("H2_FIELD_PASSWORD 未设置，无法执行远程操作")
    return password


def _ssh(host: str, command: str, timeout: float = 15) -> str:
    """Run one SSH command; a timeout does not mean failure (nohup starts can
    hang the channel after the process is up), callers verify by probing."""
    result = subprocess.run(
        ["sshpass", "-p", _password(), "ssh", "-n", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=accept-new", f"unitree@{host}", command],
        capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or "").strip()
        raise FieldError(message[:200] or f"ssh 退出码 {result.returncode}")
    return result.stdout.strip()


def _ssh_best_effort(host: str, command: str, timeout: float = 15) -> None:
    """Start-style command: tolerate a hung channel, the probe decides."""
    try:
        _ssh(host, command, timeout=timeout)
    except (FieldError, subprocess.TimeoutExpired):
        pass


def probe_pc2() -> bool:
    try:
        with urllib.request.urlopen(PC2_AGENT_URL, timeout=2) as response:
            payload = json.load(response)
        return payload.get("available") is True
    except Exception:
        return False


def port_listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def forwards_status() -> dict[str, bool]:
    return {str(port): port_listening(port) for port in FORWARD_PORTS}


def status(gateway_connected: bool) -> dict:
    """Link snapshot for the UI. The PC3 gateway binds to PC3's loopback, so
    from here it is only observable through both forwards plus the backend's
    own gateway connection."""
    forwards = forwards_status()
    return {
        "pc2_agent": probe_pc2(),
        "forwards": forwards,
        "gateway": gateway_connected and all(forwards.values()),
    }


def reconnect_forwards() -> dict:
    """Kill half-dead forward listeners and re-establish both tunnels."""
    # Matches only the ssh command lines of the two forwards, not this process.
    subprocess.run(["pkill", "-f", r"127\.0\.0\.1:1505[12]:127\.0\.0\.1:5005[12]"],
                   capture_output=True)
    time.sleep(0.5)
    for port, target in ((15051, 50051), (15052, 50052)):
        subprocess.run(
            ["sshpass", "-p", _password(), "ssh", "-N", "-T", "-f",
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5",
             "-o", "ServerAliveCountMax=2",
             "-L", f"127.0.0.1:{port}:127.0.0.1:{target}", f"unitree@{PC3}"],
            capture_output=True, text=True, timeout=20)
    time.sleep(1.5)
    forwards = forwards_status()
    return {"ok": all(forwards.values()), "forwards": forwards}


def start_pc2_agent() -> dict:
    running = _ssh(PC2, 'pgrep -f "[s]ensor_agent.py" >/dev/null && echo running || echo stopped')
    if running == "running":
        return {"ok": True, "already": True}
    _ssh_best_effort(PC2, PC2_START)
    time.sleep(2.5)
    return {"ok": probe_pc2(), "already": False}


def start_pc3_gateway() -> dict:
    running = _ssh(PC3, 'pgrep -f "[h]2_gateway.server" >/dev/null && echo running || echo stopped')
    if running == "running":
        return {"ok": True, "already": True}
    _ssh_best_effort(PC3, PC3_START, timeout=20)
    time.sleep(3.5)
    ports = _ssh(PC3, "ss -tln | grep -c '5005[12]'")
    return {"ok": ports == "2", "already": False}
