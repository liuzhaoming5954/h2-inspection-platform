"""High-level SDK2 control for a locally operated Unitree H2."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Protocol
from xml.etree import ElementTree

from h2_gateway.teleop import ACTIONS, LEASE_SECONDS, MAX_COMMAND_FUTURE_MS, TICK_SECONDS, teleop_reply


LOG = logging.getLogger(__name__)
MAIN_FSM = 601
STANDING = 0
MOVING = 1
SDK_TIMEOUT = 0.10
STATE_POLL_INTERVAL = 0.35
STATE_MAX_AGE = 0.8
DEFAULT_SENSOR_AGENT_URL = "http://192.168.124.162:8090/environment"
MAX_VELOCITY_DURATION = 0.25
MIN_VELOCITY_DURATION = 0.04

# Initial supervised trial limits. The H2's built-in controller owns gait and balance.
VELOCITIES = {
    "forward": (0.15, 0.0, 0.0),
    "backward": (-0.15, 0.0, 0.0),
    "turn_left": (0.0, 0.0, 0.25),
    "turn_right": (0.0, 0.0, -0.25),
}


class Loco(Protocol):
    def fsm(self) -> tuple[int, int | None]: ...
    def velocity(self, vx: float, vy: float, omega: float, duration: float) -> None: ...


class UnitreeLoco:
    def __init__(self, interface: str, *, domain: int = 0):
        try:
            socket.if_nametoindex(interface)
        except OSError as error:
            raise ValueError(f"Unknown H2 network interface: {interface}") from error
        try:
            from unitree_sdk2py.core import channel
            from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient
        except ImportError as error:
            raise RuntimeError("Install the Unitree H2 Python SDK and CycloneDDS first; see README.md") from error
        try:
            # The pinned SDK enables config tracing to /tmp/cdds.LOG.
            # CycloneDDS 0.10.2 aborts in that trace on the Ubuntu 24.04 build.
            original_config = channel.ChannelConfigHasInterface
            config = ElementTree.fromstring(original_config)
            for entry in config.findall("./Domain"):
                tracing = entry.find("Tracing")
                if tracing is not None:
                    entry.remove(tracing)
            channel.ChannelConfigHasInterface = ElementTree.tostring(config, encoding="unicode")
            try:
                channel.ChannelFactoryInitialize(domain, interface)
            finally:
                channel.ChannelConfigHasInterface = original_config
            self._client = LocoClient()
            self._client.SetTimeout(SDK_TIMEOUT)
            self._client.Init()
        except Exception as error:
            raise RuntimeError(f"Could not initialize Unitree DDS on {interface}: {error}") from error

    def fsm(self) -> tuple[int, int | None]:
        code, fsm_id = self._client.GetFsmId()
        if code != 0 or type(fsm_id) is not int:
            raise RuntimeError(f"H2 GetFsmId failed (SDK code {code})")
        if fsm_id != MAIN_FSM:
            return fsm_id, None
        code, fsm_mode = self._client.GetFsmMode()
        if code != 0 or type(fsm_mode) is not int:
            raise RuntimeError(f"H2 GetFsmMode failed (SDK code {code})")
        return fsm_id, fsm_mode

    def velocity(self, vx: float, vy: float, omega: float, duration: float) -> None:
        # The Python SDK's Move and StopMove convenience methods discard the RPC result.
        code = self._client.SetVelocity(vx, vy, omega, duration)
        if code != 0:
            raise RuntimeError(f"H2 SetVelocity failed (SDK code {code})")


class EnvironmentSource(Protocol):
    def sample(self) -> dict | None: ...


class BatterySource(Protocol):
    def battery(self) -> float | None: ...


class BmsWatcher:
    """Subscribe the robot's low-frequency BMS topic on the live channel factory.

    Must be constructed after ChannelFactoryInitialize (UnitreeLoco does it).
    Stale or missing readings degrade to None; the field stays absent.
    """

    def __init__(self, topic: str = "rt/lf/bmsstate", *, fresh_seconds: float = 5.0):
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import BmsState_

        self._lock = threading.Lock()
        self._fresh_seconds = fresh_seconds
        self._reading: tuple[float, float] | None = None  # (monotonic, soc percent)
        self._subscriber = ChannelSubscriber(topic, BmsState_)
        self._subscriber.Init(self._on_message, 1)

    def _on_message(self, message) -> None:
        with self._lock:
            self._reading = (time.monotonic(), float(message.soc))

    def battery(self) -> float | None:
        with self._lock:
            reading = self._reading
        if reading is None or time.monotonic() - reading[0] > self._fresh_seconds:
            return None
        return reading[1]


class AgentEnvironment:
    """Poll the PC2 RS-485 sensor agent; serve the last fresh reading.

    Read failures and staleness degrade to None (fields simply absent from
    state); telemetry never blocks or affects the motion paths.
    """

    def __init__(self, url: str, *, interval: float = 2.0, fresh_seconds: float = 8.0,
                 fetch=None):
        self._url = url
        self._interval = max(0.5, interval)
        self._fresh_seconds = fresh_seconds
        self._fetch = fetch or self._http_fetch
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reading: tuple[float, dict] | None = None  # (monotonic, values)

    def _http_fetch(self) -> dict | None:
        import urllib.request
        try:
            with urllib.request.urlopen(self._url, timeout=1.5) as response:
                payload = json.loads(response.read().decode())
        except Exception:
            return None
        sample = payload.get("sample") if isinstance(payload, dict) else None
        if not isinstance(sample, dict) or payload.get("available") is not True:
            return None
        temperature = sample.get("temperature_c")
        humidity = sample.get("humidity_percent")
        if not isinstance(temperature, (int, float)) or not isinstance(humidity, (int, float)):
            return None
        return {"temperature_c": float(temperature), "humidity_percent": float(humidity)}

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._interval):
            reading = self._fetch()
            if reading is not None:
                with self._lock:
                    self._reading = (time.monotonic(), reading)

    def start(self) -> "AgentEnvironment":
        threading.Thread(target=self._poll_loop, name="h2-environment-poll", daemon=True).start()
        return self

    def sample(self) -> dict | None:
        with self._lock:
            reading = self._reading
        if reading is None or time.monotonic() - reading[0] > self._fresh_seconds:
            return None
        return reading[1]

    def close(self) -> None:
        self._stop.set()


class H2HardwareGateway:
    """One serialized SDK writer and one non-persistent control lease."""

    def __init__(self, loco: Loco, environment: EnvironmentSource | None = None,
                 battery: BatterySource | None = None):
        self._loco = loco
        self._environment = environment
        self._battery = battery
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._fsm: tuple[int, int | None] | None = None
        self._state_at = 0.0
        self._updated_at = ""
        self._token = ""
        self._sequence = 0
        self._lease_until = self._motion_until = 0.0
        self._motion = "unknown"
        self._stop_needed = False
        self._stop_fault = False
        self._worker = threading.Thread(target=self._watch, name="h2-hardware-watchdog", daemon=True)
        self._worker.start()

    def _ready(self, now: float) -> bool:
        modes = (STANDING, MOVING) if self._token else (STANDING,)
        return (not self._shutdown.is_set() and not self._stop_fault
                and self._fsm is not None and now - self._state_at <= STATE_MAX_AGE
                and self._fsm[0] == MAIN_FSM and self._fsm[1] in modes)

    def _stop(self) -> bool:
        self._motion_until = 0.0
        try:
            self._loco.velocity(0.0, 0.0, 0.0, MAX_VELOCITY_DURATION)
        except Exception as error:
            LOG.error("H2 stop request was not acknowledged: %s", error)
            self._motion = "unknown"
            self._stop_needed = True
            self._stop_fault = True
            return False
        self._stop_needed = False
        self._motion = "stop_requested"
        return True

    def _revoke(self) -> bool:
        self._token = ""
        self._lease_until = 0.0
        self._sequence = 0
        return not self._stop_needed or self._stop()

    def _expire(self, now: float) -> None:
        if self._token and (now >= self._lease_until
                            or (self._motion_until and now >= self._motion_until)
                            or not self._ready(now)):
            self._revoke()

    def _watch(self) -> None:
        next_poll = 0.0
        while not self._shutdown.wait(TICK_SECONDS):
            with self._lock:
                if self._shutdown.is_set():
                    return
                now = time.monotonic()
                self._expire(now)
                if now < next_poll:
                    continue
                # Let stop and refreshed velocity take priority near the current motion deadline.
                if self._motion_until and self._motion_until - now <= 2 * SDK_TIMEOUT:
                    continue
                try:
                    self._fsm = self._loco.fsm()
                    self._state_at = time.monotonic()
                    self._updated_at = datetime.now(timezone.utc).isoformat()
                except Exception as error:
                    if self._fsm is not None:
                        LOG.warning("Lost H2 high-level state: %s", error)
                    self._fsm = None
                    self._motion = "unknown"
                self._expire(time.monotonic())
                next_poll = time.monotonic() + STATE_POLL_INTERVAL

    def get_state(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            ready = self._ready(now)
            fresh = self._fsm is not None and now - self._state_at <= STATE_MAX_AGE
            if self._stop_fault:
                status = "fault"
            elif not fresh:
                status = "offline"
            elif self._token:
                status = "teleop"
            else:
                status = "ready" if ready else "not_ready"
            state = {
                "robot_id": "H2", "simulated": False, "status": status,
                "teleop_ready": ready, "teleop_active": bool(self._token),
                "updated_at": self._updated_at, "motion": self._motion if fresh else "unknown",
            }
            if fresh:
                fsm_id, fsm_mode = self._fsm
                state["fsm_id"] = fsm_id
                if fsm_mode is not None:
                    state["fsm_mode"] = fsm_mode
            if self._environment is not None:
                reading = self._environment.sample()
                if reading is not None:
                    state["temperature_c"] = reading["temperature_c"]
                    state["humidity_percent"] = reading["humidity_percent"]
            if self._battery is not None:
                percent = self._battery.battery()
                if percent is not None:
                    state["battery_percent"] = percent
            return state

    def teleop_acquire(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            if self._stop_fault:
                return teleop_reply(False, "stop_unconfirmed", "previous H2 stop was not acknowledged")
            if self._token:
                return teleop_reply(False, "teleop_busy", "another operator holds H2 control")
            if self._fsm is None or now - self._state_at > STATE_MAX_AGE or self._shutdown.is_set():
                return teleop_reply(False, "hardware_unavailable", "no fresh H2 motion service state")
            if not self._ready(now):
                return teleop_reply(False, "robot_not_ready", "H2 must be standing in main locomotion mode")
            self._token = secrets.token_urlsafe(24)
            self._lease_until = now + LEASE_SECONDS
            return teleop_reply(True, "acquired", "H2 teleop control acquired", self._token)

    def teleop_command(self, token: str, sequence: int, action: str, expires_at_ms: int) -> dict:
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            if self._stop_fault:
                return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
            if not self._token or token != self._token:
                return teleop_reply(False, "invalid_token", "H2 teleop control is no longer held")
            if action not in ACTIONS:
                return teleop_reply(False, "invalid_action", "unsupported H2 teleop action")
            if sequence <= self._sequence:
                return teleop_reply(False, "stale_sequence", "teleop sequence must strictly increase")
            remaining_ms = expires_at_ms - time.time_ns() // 1_000_000
            # An ordered stop from the current owner must survive clock skew
            # between the workstation and PC3. Direction/heartbeat still expire.
            if action != "stop":
                if remaining_ms > MAX_COMMAND_FUTURE_MS:
                    return teleop_reply(False, "invalid_expiry", "teleop deadline is too far in the future")
                if remaining_ms <= 0:
                    return teleop_reply(False, "expired", "teleop command has expired")
            self._sequence = sequence
            self._lease_until = now + LEASE_SECONDS
            if action == "heartbeat":
                return teleop_reply(True, "renewed", "H2 teleop lease renewed", token)
            if action == "stop":
                if self._stop():
                    return teleop_reply(True, "stopped", "H2 acknowledged zero velocity", token)
                self._token = ""
                return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")

            duration = min(remaining_ms / 1000, MAX_VELOCITY_DURATION)
            if duration < MIN_VELOCITY_DURATION:
                return teleop_reply(False, "expired", "too little validity remains to send H2 motion")
            # Once sent, an RPC timeout cannot distinguish a lost response from lost delivery.
            self._stop_needed = True
            self._motion_until = now + duration
            try:
                self._loco.velocity(*VELOCITIES[action], duration)
            except Exception as error:
                LOG.warning("H2 velocity request was not acknowledged: %s", error)
                if not self._revoke():
                    return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
                return teleop_reply(False, "sdk_rejected", "H2 velocity request was not acknowledged")
            if time.monotonic() >= self._motion_until:
                if not self._revoke():
                    return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
                return teleop_reply(False, "invalid_token", "H2 velocity acknowledgement arrived after its deadline")
            self._motion = action
            return teleop_reply(True, "applied", "H2 acknowledged velocity request", token)

    def teleop_release(self, token: str) -> dict:
        with self._lock:
            if self._stop_fault:
                return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
            if not self._token or token != self._token:
                return teleop_reply(False, "invalid_token", "H2 teleop control is no longer held")
            if not self._revoke():
                return teleop_reply(False, "stop_unconfirmed", "H2 stop was not acknowledged")
            return teleop_reply(True, "released", "H2 teleop control released")

    def close(self) -> None:
        self._shutdown.set()
        with self._lock:
            self._revoke()
        self._worker.join()
        if self._environment is not None:
            self._environment.close()


def probe() -> None:
    parser = argparse.ArgumentParser(description="Read H2 high-level FSM status without sending motion")
    parser.add_argument("--interface", required=True, help="Network interface connected to the H2")
    args = parser.parse_args()
    try:
        fsm_id, fsm_mode = UnitreeLoco(args.interface).fsm()
    except (RuntimeError, ValueError) as error:
        raise SystemExit(f"H2 status query failed: {error}") from error
    print(json.dumps({"fsm_id": fsm_id, "fsm_mode": fsm_mode,
                      "teleop_ready": (fsm_id, fsm_mode) == (MAIN_FSM, STANDING)}))


if __name__ == "__main__":
    probe()
