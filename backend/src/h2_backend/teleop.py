import asyncio
from contextlib import suppress
from ipaddress import ip_address
import time
from typing import Any, Literal

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError


UI_ORIGINS = frozenset(
    f"http://{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (5173, 4173)
)
LOCAL_HOSTS = frozenset(
    f"{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (5173, 4173, 8000)
)
COMMAND_TTL_MS = 350
IDLE_TIMEOUT_SECONDS = 2.0
MAX_FRAME_LENGTH = 512
MAX_SAFE_INTEGER = 2**53 - 1


class TeleopInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    action: Literal["forward", "backward", "turn_left", "turn_right", "stop", "heartbeat", "release"]
    sequence: int = Field(gt=0, le=MAX_SAFE_INTEGER)
    sent_at_ms: int = Field(ge=0, le=MAX_SAFE_INTEGER)


# Send fixed public messages. Gateway replies can contain a private lease token
# or diagnostic text that should never appear in a browser frame.
MESSAGES = {
    "acquired": "已获得模拟遥控控制权。",
    "applied": "已应用模拟遥控指令。",
    "stopped": "已停止模拟机器人运动。",
    "renewed": "已续期模拟遥控控制权。",
    "released": "已释放模拟遥控控制权。",
    "teleop_busy": "已有操作员获得遥控控制权。",
    "robot_busy": "机器人正在巡检，无法进入遥控模式。",
    "invalid_token": "遥控控制权已失效，请重新获取。",
    "stale_sequence": "指令序号未递增，指令未执行。",
    "expired": "网关收到的指令已过期，未执行。",
    "invalid_expiry": "网关拒绝了指令有效期。",
    "invalid_action": "网关拒绝了不支持的动作。",
    "stale_command": "指令已过期，未发送给网关。",
    "future_command": "指令时间晚于服务器时间，未发送给网关。",
    "invalid_message": "遥控消息格式不正确。",
    "simulation_required": "当前入口仅支持模拟机器人遥控。",
    "hardware_required": "真机遥控需要连接 H2 真机网关。",
    "robot_not_ready": "H2 高层运控尚未就绪。",
    "hardware_unavailable": "H2 高层运控不可用，遥控会话已结束。",
    "sdk_rejected": "H2 未确认运动指令，遥控会话已结束。",
    "stop_unconfirmed": "H2 未确认停步请求，请在现场确认机器人状态。",
    "gateway_timeout": "网关响应超时，遥控会话已结束。",
    "gateway_unavailable": "无法连接模拟机器人网关。",
    "gateway_protocol_error": "网关未返回有效的模拟遥控控制权。",
    "gateway_rejected": "网关拒绝了遥控请求。",
    "idle_timeout": "遥控会话长时间未收到指令，请重新获取控制权。",
    "ok": "网关已确认遥控请求。",
}
HARDWARE_MESSAGES = {
    "acquired": "已获得 H2 真机遥控控制权。",
    "applied": "H2 已接收运动指令。",
    "stopped": "H2 已接收停步请求。",
    "renewed": "已续期 H2 真机遥控控制权。",
    "released": "已释放 H2 真机遥控控制权。",
    "gateway_unavailable": "无法连接 H2 真机网关，遥控会话已结束。",
    "gateway_protocol_error": "H2 真机网关返回的遥控响应无效，遥控会话已结束。",
}
HARDWARE_FAILURES = frozenset({"robot_not_ready", "hardware_unavailable", "sdk_rejected", "stop_unconfirmed"})
HARDWARE_ACKS = {"heartbeat": "renewed", "release": "released", "stop": "stopped"}
VALIDATION_ERRORS = frozenset({"stale_sequence", "expired", "invalid_expiry", "invalid_action"})
GATEWAY_CODES = frozenset({
    "acquired", "applied", "stopped", "renewed", "released", "teleop_busy", "robot_busy",
    "invalid_token", "stale_sequence", "expired", "invalid_expiry", "invalid_action",
}) | HARDWARE_FAILURES


def trusted_ui(websocket: WebSocket) -> bool:
    origins = websocket.headers.getlist("origin")
    hosts = websocket.headers.getlist("host")
    forwarded = websocket.headers.getlist("x-forwarded-host")
    return (len(origins) == 1 and origins[0] in UI_ORIGINS and len(hosts) == 1
            and all(host.lower() in LOCAL_HOSTS for host in hosts + forwarded))


def loopback_peer(websocket: WebSocket) -> bool:
    if websocket.client is None:
        return False
    try:
        return ip_address(websocket.client.host).is_loopback
    except ValueError:
        return False


def gateway_outcome(result: dict[str, Any]) -> tuple[bool, str]:
    accepted = result.get("accepted") is True
    code = result.get("code")
    if not isinstance(code, str) or code not in GATEWAY_CODES:
        code = "ok" if accepted else "gateway_rejected"
    return accepted, code


def transport_code(error: Exception) -> str:
    status = getattr(error, "code", None)
    if isinstance(error, TimeoutError) or (callable(status) and getattr(status(), "name", None) == "DEADLINE_EXCEEDED"):
        return "gateway_timeout"
    return "gateway_unavailable"


async def teleop_socket(websocket: WebSocket, gateway: Any, *, hardware_teleop: bool = False) -> None:
    if not trusted_ui(websocket) or (hardware_teleop and not loopback_peer(websocket)):
        await websocket.close(code=1008)
        return

    async def send(kind: str, accepted: bool, code: str, sequence: int | None = None) -> None:
        message = HARDWARE_MESSAGES.get(code, MESSAGES[code]) if hardware_teleop else MESSAGES[code]
        body: dict[str, Any] = {"type": kind, "accepted": accepted, "code": code, "message": message}
        if sequence is not None:
            body["sequence"] = sequence
        await websocket.send_json(body)

    await websocket.accept()
    token = None
    try:
        try:
            state = await asyncio.to_thread(gateway.get_state)
            denied = None
            expected_simulated = not hardware_teleop
            if state.get("simulated") is not expected_simulated:
                denied = "hardware_required" if hardware_teleop else "simulation_required"
            elif hardware_teleop:
                if state.get("status") in {"offline", "fault"}:
                    denied = "hardware_unavailable"
                elif state.get("teleop_ready") is not True or state.get("status") not in {"ready", "teleop"}:
                    denied = "robot_not_ready"
            if denied:
                await send("lease", False, denied)
                await websocket.close(code=1008)
                return
            result = await asyncio.to_thread(gateway.teleop_acquire)
            accepted, code = gateway_outcome(result)
        except WebSocketDisconnect:
            raise
        except Exception as error:
            await send("lease", False, transport_code(error))
            await websocket.close(code=1011)
            return

        if not accepted:
            await send("lease", False, code)
            await websocket.close(code=1008)
            return
        candidate = result.get("token")
        if not isinstance(candidate, str) or not candidate:
            await send("lease", False, "gateway_protocol_error")
            await websocket.close(code=1011)
            return
        token = candidate
        if hardware_teleop and code != "acquired":
            await send("lease", False, "gateway_protocol_error")
            await websocket.close(code=1011)
            return
        await send("lease", True, code)

        while True:
            try:
                frame = await asyncio.wait_for(websocket.receive(), timeout=IDLE_TIMEOUT_SECONDS)
            except TimeoutError:
                await send("error", False, "idle_timeout")
                await websocket.close(code=1008)
                return
            if frame["type"] == "websocket.disconnect":
                return
            try:
                raw = frame.get("text")
                if not isinstance(raw, str) or len(raw) > MAX_FRAME_LENGTH:
                    raise ValueError("Invalid text frame")
                command = TeleopInput.model_validate_json(raw)
            except (ValidationError, ValueError):
                await send("error", False, "invalid_message")
                continue

            now_ms = time.time_ns() // 1_000_000
            if command.action in ("stop", "release"):
                expires_at_ms = now_ms + COMMAND_TTL_MS
            else:
                if command.sent_at_ms > now_ms:
                    await send("ack", False, "future_command", command.sequence)
                    continue
                expires_at_ms = command.sent_at_ms + COMMAND_TTL_MS
                if expires_at_ms <= now_ms:
                    await send("ack", False, "stale_command", command.sequence)
                    continue

            try:
                if command.action == "release":
                    result = await asyncio.to_thread(gateway.teleop_release, token)
                else:
                    result = await asyncio.to_thread(
                        gateway.teleop_command, token, command.sequence, command.action, expires_at_ms,
                    )
                accepted, code = gateway_outcome(result)
            except Exception as error:
                await send("error", False, transport_code(error), command.sequence)
                await websocket.close(code=1011)
                return

            if hardware_teleop and accepted:
                expected = HARDWARE_ACKS.get(command.action, "applied")
                if code != expected:
                    await send("error", False, "gateway_protocol_error", command.sequence)
                    await websocket.close(code=1011)
                    return
            if code == "invalid_token" or (command.action == "release" and accepted):
                token = None
            await send("ack", accepted, code, command.sequence)
            if code == "invalid_token" or command.action == "release" or (
                hardware_teleop and not accepted and (code not in VALIDATION_ERRORS or command.action == "stop")
            ):
                await websocket.close(code=1000 if accepted else 1008)
                return
    except WebSocketDisconnect:
        pass
    finally:
        if token is not None:
            with suppress(Exception):
                await asyncio.to_thread(gateway.teleop_release, token)
