"""管理仅保存在内存中的模拟运动，所有访问由引擎串行处理。"""

from __future__ import annotations

import math
import secrets
from typing import Any


LEASE_SECONDS = 2.0
MAX_COMMAND_FUTURE_MS = 500
MAX_MOTION_SECONDS = 0.45
TICK_SECONDS = 0.02

# 这些数值只影响模拟场景中的机器人位置。
LINEAR_SPEED = 0.35
TURN_SPEED = 0.8
DIRECTIONS = frozenset({"forward", "backward", "turn_left", "turn_right"})
ACTIONS = DIRECTIONS | {"stop", "heartbeat"}


def teleop_reply(accepted: bool, code: str, message: str, token: str = "") -> dict[str, Any]:
    return {"accepted": accepted, "code": code, "message": message, "token": token}


class SimulatedTeleop:
    """仅在内存中维护租约和超时停步计时器。"""

    def __init__(self) -> None:
        self.x = self.y = self.yaw = 0.0
        self.last_tick = 0.0
        self.reset()

    def reset(self) -> None:
        self.token = ""
        self.motion = "stop"
        self.lease_deadline = self.motion_deadline = 0.0
        self.sequence = 0

    def acquire(self, now: float) -> dict[str, Any]:
        self.token = secrets.token_urlsafe(24)
        self.last_tick = now
        self.lease_deadline = now + LEASE_SECONDS
        return teleop_reply(True, "acquired", "simulated teleop control acquired", self.token)

    def pose(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "yaw": self.yaw}

    def inspection_position(self, x: float, y: float) -> None:
        if (x, y) != (self.x, self.y):
            self.yaw = math.atan2(y - self.y, x - self.x)
        self.x, self.y = x, y

    def advance(self, now: float) -> bool:
        """按已过去的时间结算运动，积分上限为两个截止时间中的较早者。"""
        if not self.token:
            return False
        changed = False
        if self.motion in DIRECTIONS:
            elapsed = max(0.0, min(now, self.lease_deadline, self.motion_deadline) - self.last_tick)
            if elapsed:
                if self.motion in {"forward", "backward"}:
                    displacement = LINEAR_SPEED * elapsed * (1 if self.motion == "forward" else -1)
                    self.x += displacement * math.cos(self.yaw)
                    self.y += displacement * math.sin(self.yaw)
                else:
                    angle = TURN_SPEED * elapsed * (1 if self.motion == "turn_left" else -1)
                    self.yaw = math.remainder(self.yaw + angle, 2 * math.pi)
                changed = True
        self.last_tick = now
        if now >= self.lease_deadline or (self.motion in DIRECTIONS and now >= self.motion_deadline):
            self.reset()
            changed = True
        return changed

    def command(self, token: str, sequence: int, action: str, remaining_ms: int,
                now: float) -> dict[str, Any]:
        if not self.token or token != self.token:
            return teleop_reply(False, "invalid_token", "simulated teleop control is no longer held")
        if action not in ACTIONS:
            return teleop_reply(False, "invalid_action", "unsupported simulated teleop action")
        if sequence <= self.sequence:
            return teleop_reply(False, "stale_sequence", "teleop sequence must strictly increase")
        if remaining_ms <= 0:
            return teleop_reply(False, "expired", "teleop command has expired")
        if remaining_ms > MAX_COMMAND_FUTURE_MS:
            return teleop_reply(False, "invalid_expiry", "teleop deadline is too far in the future")
        self.sequence = sequence
        self.lease_deadline = now + LEASE_SECONDS
        if action == "heartbeat":
            return teleop_reply(True, "renewed", "simulated teleop lease renewed", self.token)
        self.motion = action
        self.motion_deadline = now + min(remaining_ms / 1000, MAX_MOTION_SECONDS) if action in DIRECTIONS else 0.0
        code = "stopped" if action == "stop" else "applied"
        return teleop_reply(True, code, f"simulated teleop {action}", self.token)

    def release(self, token: str) -> dict[str, Any]:
        if not self.token or token != self.token:
            return teleop_reply(False, "invalid_token", "simulated teleop control is no longer held")
        self.reset()
        return teleop_reply(True, "released", "simulated teleop control released")
